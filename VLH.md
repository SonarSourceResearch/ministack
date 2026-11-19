# SonarSourceResearch fork of MiniStack

This fork exists so vortex-long-horizon can run its cloud harness against MiniStack's ECS with real
multi-container tasks. Upstream is `ministackorg/ministack`; nothing here is proposed upstream yet
(contribution is tracked in AIRES-10940).

- **Base:** upstream `v1.5.40` (2026-11-11), the newest release that was seven days old when the fork was made.
- **Branch:** `vlh/ecs-patches`. Releases are tags `v<upstream>-vlh.<n>`; pin a tag, never the branch.
- **Patches:** one commit on the base, touching `ministack/services/ecs.py`, one line in
  `ministack/app.py`, and the new `tests/test_ecs_containers.py`.

## What the patch changes

ECS `RunTask` and service tasks, when MiniStack starts real containers:

1. **`entryPoint`** is passed to Docker. ECS has no entryPoint override at RunTask time (the API's
   `ContainerOverride` has `command`, not `entryPoint`), so only the task definition's is read.
2. **`dependsOn`** with `START`, `COMPLETE`, `SUCCESS` and `HEALTHY`. Containers start in dependency
   order, whatever the listing order, and a container waits until its dependencies are in their
   condition (up to its `startTimeout` if set, otherwise until the task is stopped). A `SUCCESS` dependency that
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
