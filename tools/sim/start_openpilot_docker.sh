#!/bin/bash

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null && pwd)"
cd $DIR

# expose X to the container
xhost +local:root

OPENPILOT_DIR="/openpilot"
if ! [[ -z "$MOUNT_OPENPILOT" ]]
then
  OPENPILOT_DIR="$(dirname $(dirname $DIR))"
  EXTRA_ARGS="-v $OPENPILOT_DIR:$OPENPILOT_DIR -e PYTHONPATH=$OPENPILOT_DIR:$PYTHONPATH"
fi

if [[ "$CI" ]]
then
  CMD="cd ${OPENPILOT_DIR}/tools/sim/test && CI=1 ./test_carla_integration.py $*"
else
  docker pull ghcr.io/commaai/openpilot-sim:latest
  CMD="./tmux_script.sh $*"
  EXTRA_ARGS="${EXTRA_ARGS} -it"
fi

docker run --net=host\
  --name openpilot_client \
  --rm \
  --gpus all \
  --device=/dev/dri:/dev/dri \
  --device=/dev/input:/dev/input \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  --shm-size 1G \
  -e DISPLAY=$DISPLAY \
  -e QT_X11_NO_MITSHM=1 \
  -w "$OPENPILOT_DIR/tools/sim" \
  $EXTRA_ARGS \
  ghcr.io/commaai/openpilot-sim:latest \
  /bin/bash -c "$CMD"
