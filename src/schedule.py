import json
from datetime import datetime, timedelta, timezone

import botocore.exceptions
import jmespath as jp
from croniter import croniter
from mypy_boto3_events import EventBridgeClient
from mypy_boto3_events import type_defs as events_type_defs
from mypy_boto3_scheduler import EventBridgeSchedulerClient
from mypy_boto3_scheduler import type_defs as scheduler_type_defs
from pydantic import ValidationError

import config
import entities
import sso
from events import DiscardButtonsEvent, Event, RevokeEvent, ScheduledRevokeEvent

logger = config.get_logger(service="schedule")
cfg = config.get_config()


def get_event_brige_rule(event_brige_client: EventBridgeClient, rule_name: str) -> events_type_defs.DescribeRuleResponseTypeDef:
    return event_brige_client.describe_rule(Name=rule_name)


def get_next_cron_run_time(cron_expression: str, base_time: datetime) -> datetime:
    # Replace ? with * to comply with croniter
    cron_expression = cron_expression.replace("?", "*")
    cron_iter = croniter(cron_expression, base_time)
    next_run_time = cron_iter.get_next(datetime)
    logger.debug(f"Next run time: {next_run_time}")
    return next_run_time


def check_rule_expression_and_get_next_run(rule: events_type_defs.DescribeRuleResponseTypeDef) -> datetime or str:
    schedule_expression = rule["ScheduleExpression"]
    current_time = datetime.utcnow()
    logger.debug(f"Current time: {current_time}")
    logger.debug(f"Schedule expression: {schedule_expression}")

    if schedule_expression.startswith("rate"):
        return get_next_rate_run_time(rule, current_time)
    elif schedule_expression.startswith("cron"):
        clean_expression = schedule_expression.replace("cron(", "").replace(")", "")
        return get_next_cron_run_time(clean_expression, current_time)
    else:
        raise ValueError("Unknown schedule expression format!")


def get_next_rate_run_time(rule: events_type_defs.DescribeRuleResponseTypeDef, current_time: datetime) -> datetime:
    # Extract creation timestamp from description
    creation_time_str = rule["Description"].split(": ")[-1]
    creation_time = datetime.strptime(creation_time_str, "%Y-%m-%dT%H:%M:%SZ")

    # Extract rate from schedule expression
    rate_expression = rule["ScheduleExpression"].split("(")[1].split(")")[0]
    rate_value, rate_unit = int(rate_expression.split()[0]), rate_expression.split()[1]

    # Initialize rate as timedelta
    rate = timedelta()

    if rate_unit.startswith("minute"):
        rate = timedelta(minutes=rate_value)
    elif rate_unit.startswith("hour"):
        rate = timedelta(hours=rate_value)
    elif rate_unit.startswith("day"):
        rate = timedelta(days=rate_value)

    # Calculate next run time
    time_since_last_run = current_time - creation_time
    runs_since_creation = time_since_last_run // rate
    next_run_time = creation_time + rate * (runs_since_creation + 1)

    logger.debug(f"Next run time: {next_run_time}")
    return next_run_time


def get_schedules(client: EventBridgeSchedulerClient) -> list[scheduler_type_defs.GetScheduleOutputTypeDef]:
    paginator = client.get_paginator("list_schedules")
    scheduled_events = []
    for page in paginator.paginate(GroupName=cfg.schedule_group_name):
        schedules_names = jp.search("Schedules[*].Name", page)
        for schedule_name in schedules_names:
            if not schedule_name:
                continue
            full_schedule = client.get_schedule(GroupName=cfg.schedule_group_name, Name=schedule_name)
            scheduled_events.append(full_schedule)
    return scheduled_events


def get_scheduled_events(client: EventBridgeSchedulerClient) -> list[ScheduledRevokeEvent]:
    scheduled_events = get_schedules(client)
    scheduled_revoke_events: list[ScheduledRevokeEvent] = []
    for full_schedule in scheduled_events:
        if full_schedule["Name"].startswith("discard-buttons"):
            continue

        event = json.loads(jp.search("Target.Input", full_schedule))

        try:
            event = Event.parse_obj(event)
        except ValidationError as e:
            logger.warning("Got unexpected event", extra={"event": event, "error": e})
            continue

        if isinstance(event.__root__, ScheduledRevokeEvent):
            scheduled_revoke_events.append(event.__root__)
            print("case: ScheduledRevokeEvent")

    return scheduled_revoke_events


def delete_schedule(client: EventBridgeSchedulerClient, schedule_name: str) -> None:
    try:
        client.delete_schedule(GroupName=cfg.schedule_group_name, Name=schedule_name)
        logger.info("Schedule deleted", extra={"schedule_name": schedule_name})
    except botocore.exceptions.ClientError as e:
        if jp.search("Error.Code", e.response) == "ResourceNotFoundException":
            logger.info("Schedule for deletion was not found", extra={"schedule_name": schedule_name})
        else:
            raise e


def get_and_delete_scheduled_revoke_event_if_already_exist(
    client: EventBridgeSchedulerClient,
    user_account_assignment: sso.UserAccountAssignment,
) -> None:
    for scheduled_event in get_scheduled_events(client):
        if scheduled_event.revoke_event.user_account_assignment == user_account_assignment:
            logger.info("Schedule already exist, deleting it", extra={"schedule_name": scheduled_event.revoke_event.schedule_name})
            delete_schedule(client, scheduled_event.revoke_event.schedule_name)


def event_bridge_schedule_after(td: timedelta) -> str:
    now = datetime.now(timezone.utc)
    return f"at({(now + td).replace(microsecond=0).isoformat().replace('+00:00', '')})"


def schedule_revoke_event(
    schedule_client: EventBridgeSchedulerClient,
    permission_duration: timedelta,
    approver: entities.slack.User,
    requester: entities.slack.User,
    user_account_assignment: sso.UserAccountAssignment,
) -> scheduler_type_defs.CreateScheduleOutputTypeDef:
    logger.info("Scheduling revoke event")
    schedule_name = f"{cfg.revoker_function_name}" + datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    get_and_delete_scheduled_revoke_event_if_already_exist(schedule_client, user_account_assignment)
    revoke_event = RevokeEvent(
        schedule_name=schedule_name,
        approver=approver,
        requester=requester,
        user_account_assignment=user_account_assignment,
        permission_duration=permission_duration,
    )
    logger.debug("Creating schedule", extra={"revoke_event": revoke_event})
    return schedule_client.create_schedule(
        FlexibleTimeWindow={"Mode": "OFF"},
        Name=schedule_name,
        GroupName=cfg.schedule_group_name,
        ScheduleExpression=event_bridge_schedule_after(permission_duration),
        State="ENABLED",
        Target=scheduler_type_defs.TargetTypeDef(
            Arn=cfg.revoker_function_arn,
            RoleArn=cfg.schedule_policy_arn,
            Input=json.dumps(
                {
                    "action": "event_bridge_revoke",
                    "revoke_event": revoke_event.json(),
                },
            ),
        ),
    )


def schedule_discard_buttons_event(
    permission_duration: timedelta,
    schedule_client: EventBridgeSchedulerClient,
    time_stamp: str,
    channel_id: str,
) -> scheduler_type_defs.CreateScheduleOutputTypeDef:
    permission_duration = timedelta(hours=2)

    logger.info("Scheduling discard buttons event")
    schedule_name = "discard-buttons" + datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    logger.debug(
        "Creating schedule",
        extra={
            "schedule_name": schedule_name,
            "permission_duration": permission_duration,
            "time_stamp": time_stamp,
            "channel_id": channel_id,
        },
    )
    return schedule_client.create_schedule(
        FlexibleTimeWindow={"Mode": "OFF"},
        Name=schedule_name,
        GroupName=cfg.schedule_group_name,
        ScheduleExpression=event_bridge_schedule_after(permission_duration),
        State="ENABLED",
        Target=scheduler_type_defs.TargetTypeDef(
            Arn=cfg.revoker_function_arn,
            RoleArn=cfg.schedule_policy_arn,
            Input=json.dumps(
                DiscardButtonsEvent(
                    action="discard_buttons_event",
                    schedule_name=schedule_name,
                    time_stamp=time_stamp,
                    channel_id=channel_id,
                ).dict()
            ),
        ),
    )
