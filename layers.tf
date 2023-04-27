module "sso_elevator_dependencies" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "4.16.0"

  create_layer    = true
  create_function = false
  layer_name      = "sso_elevator_dependencies"
  description     = "powertools-pydantic/boto3/slack_bolt"

  compatible_runtimes = ["python3.10"]
  build_in_docker     = var.build_in_docker
  runtime             = "python3.10"
  docker_image        = "lambda/python:3.10"
  docker_file         = "${path.module}/src/docker/Dockerfile"
  source_path = [{
    poetry_install = true
    path           = "${path.module}/layer"
    patterns       = ["!python/.venv/.*"]
    prefix_in_zip  = "python"
  }]
}
