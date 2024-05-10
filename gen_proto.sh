#!/bin/sh
protoc \
    -Iproto_def \
    --python_out=. \
    proto_def/meshtastic/admin.proto \
    proto_def/meshtastic/apponly.proto \
    proto_def/meshtastic/atak.proto \
    proto_def/meshtastic/cannedmessages.proto \
    proto_def/meshtastic/channel.proto \
    proto_def/meshtastic/clientonly.proto \
    proto_def/meshtastic/config.proto \
    proto_def/meshtastic/connection_status.proto \
    proto_def/meshtastic/localonly.proto \
    proto_def/meshtastic/mesh.proto \
    proto_def/meshtastic/module_config.proto \
    proto_def/meshtastic/mqtt.proto \
    proto_def/meshtastic/paxcount.proto \
    proto_def/meshtastic/portnums.proto \
    proto_def/meshtastic/remote_hardware.proto \
    proto_def/meshtastic/rtttl.proto \
    proto_def/meshtastic/storeforward.proto \
    proto_def/meshtastic/telemetry.proto \
    proto_def/meshtastic/xmodem.proto
