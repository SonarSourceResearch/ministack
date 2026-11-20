# SonarSourceResearch fork of MiniStack

This fork exists so vortex-long-horizon can run its cloud harness against MiniStack's ECS with real
multi-container tasks. Upstream is `ministackorg/ministack`; nothing here is proposed upstream yet
(contribution is tracked in AIRES-10940).

- **Base:** upstream `v1.5.40` (2026-11-11), the newest release that was seven days old when the fork was made.
- **Branch:** `vlh/ecs-patches`. Releases are tags `v<upstream>-vlh.<n>`; pin a tag, never the branch.
- **Patches:** the ECS commit on the base (plus this note and the removal of upstream's three publish
  workflows, which would try to push images and a package under upstream's names on a tag), touching `ministack/services/ecs.py`, one line in
  `ministack/app.py`, and the new `tests/test_ecs_containers.py`; then the fixes under "Other fixes".

## What the patch changes

ECS `RunTask` and service tasks, when MiniStack starts real containers:

1. **`entryPoint`** is passed to Docker. ECS has no entryPoint override at RunTask time (the API's
   `ContainerOverride` has `command`, not `entryPoint`), so only the task definition's is read.
2. **`dependsOn`** with `START`, `COMPLETE`, `SUCCESS` and `HEALTHY`. Containers start in dependency
   order, whatever the listing order, and a container waits until its dependencies are in their
   condition (up to the dependency's `startTimeout` if set, otherwise until the task is stopped). A `SUCCESS` dependency that
   exits non-zero, a `HEALTHY` one that goes unhealthy or exits, an undefined name or a cycle stops the
   task with `TaskFailedToStart`. An essential container that exits while another is still waiting stops the
   task with `EssentialContainerExited`. The task stays `ACTIVATING` until its last container starts.
3. **Task volumes with no host path** become one Docker volume per task, mounted `nocopy` into every
   container that names it (read-only where `readOnly` is set), and removed with the task. Volumes with a
   host `sourcePath` are bind mounts as before; EFS, FSx and Docker-driver volumes stay unmodelled.
4. **One network namespace per awsvpc task.** A task of two or more containers starts a pause container
   first; the others join its namespace, so they share one address, that address is the attachment's, and
   it survives the first container exiting. One-container and non-awsvpc tasks are unchanged. Needs
   MiniStack to run as a container on a Docker network (it reads its own address from there), and the
   pause image: `registry.k8s.io/pause:3.10`, or `ECS_PAUSE_IMAGE`. An image already in the daemon is not pulled.
5. **`linuxParameters.initProcessEnabled`** starts the container with Docker's init.
6. **`healthCheck`** reaches Docker's healthcheck (needed for `HEALTHY`).
7. **Essential exit** stops the task's other containers (they were left running) and the pause container.

## Other fixes

- **Cloud Map `CreateService`** reads the namespace from `DnsConfig.NamespaceId` when the request has no
  top-level `NamespaceId`. AWS accepts both; the Terraform AWS provider, and Pulumi's that bridges it, send
  only the first, so a `pulumi up` declaring a DNS service failed with `NamespaceId is required`.
  Test: `tests/test_servicediscovery.py::test_servicediscovery_create_service_with_namespace_in_dns_config`.
- **CodeBuild's "docker" executor** (`MINISTACK_CODEBUILD_EXECUTE=1` with
  `MINISTACK_CODEBUILD_EXECUTOR=docker`). Upstream's executed builds hand the buildspec to AWS's local agent,
  which starts the build container on a network of its own and passes it no flags, so a build cannot reach
  MiniStack or the containers it started by name. This executor runs the project's image itself, on the
  network MiniStack is on: the buildspec's `install`, `pre_build`, `build` and `post_build` phases run in one
  shell from a generated script (a failed command fails its phase and the later ones, except `post_build`
  after a failed `build`), `env.variables` are exported unless a project or StartBuild variable of the same
  name is set, and `env.secrets-manager`, `env.parameter-store`, `env.exported-variables` and a phase's
  `finally`, `on-failure` and `run-as` are refused. The service role reaches the build from MiniStack's
  container credentials endpoint (`AWS_CONTAINER_CREDENTIALS_FULL_URI`) unless the project declares keys, with
  `AWS_ENDPOINT_URL` at MiniStack unless the project names one; a
  `SECRETS_MANAGER` or `PARAMETER_STORE` environment variable is resolved, and one that does not resolve
  faults the build. A privileged build gets a Docker volume at `/var/lib/docker`, removed with the build,
  so a daemon it starts can use overlay2. `CODEBUILD_DOCKER_FLAGS` apply to the build container under this
  executor, bind mounts included, and its `--network` is a second network the build joins before its first
  command. Tests: `tests/test_codebuild.py`, the `docker_executor` and `phase_script` tests.
- **`ECR_LAYER_DOWNLOAD_ENDPOINT`** points `GetDownloadUrlForLayer` at MiniStack's own registry port
  (`http://ministack:4566/v2/...?ns=<registry host>`, the hint naming the tenant) instead of `https://<account>.dkr.ecr.<region>.amazonaws.com/v2/...`,
  which a client reaches only through a TLS front whose certificate it trusts. Tests:
  `tests/test_ecr.py::test_layer_download_url_*`.
- **What ECS sets in every container:** `AWS_REGION`, `AWS_DEFAULT_REGION` and `AWS_EXECUTION_ENV`
  (`AWS_ECS_FARGATE` or `AWS_ECS_EC2`), unless the container definition sets them. An SDK client made
  without a region failed in a MiniStack task and works on Fargate.
- **Task ids of 32 hexadecimal characters**, as ECS gives them, in place of a dashed UUID: a caller that
  keys something on the id checks its form.
- **`ECS_CONTAINER_CREDENTIALS=relative`** hands task credentials over as Fargate does:
  `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`, a path its clients fetch from `169.254.170.2:80`, in place of
  `AWS_CONTAINER_CREDENTIALS_FULL_URI` at MiniStack's address. Whoever runs MiniStack routes that address to
  MiniStack's port, as ECS's agent answers there. A client that reads only the relative URI, or takes
  credentials only from that address, had none.
- **CodeBuild `UpdateProject` and `DeleteProject` take the project's ARN** as well as its name. The
  Terraform provider sends the ARN, so `pulumi destroy` of a project failed with `Project not found`.
- Tests for the four: `tests/test_ecs_containers.py` (`region_and_launch_type`, `task_id`,
  `credentials_as_fargate`) and `tests/test_codebuild.py::test_codebuild_update_and_delete_take_the_project_arn`.

## Running the tests

`tests/test_ecs_containers.py` has two halves. The first drives the task worker against a recording Docker
double and needs no daemon (these also run in upstream's CI shape, `DOCKER_HOST` pointed nowhere). The second
starts real containers (`data_plane`), needs a Docker daemon, and expects MiniStack itself to be a container
on a user-defined network so the pause container applies:

    uv pip install -e ".[test]" -r requirements.txt
    pytest tests/test_ecs_containers.py            # MINISTACK_ENDPOINT points at the running container

## Not in the patch

CPU and memory limits, `volumesFrom`, `stopTimeout`, ECS task state change events, and the Docker Desktop
fallback (a MiniStack run on the host rather than in a container keeps one address per container).
