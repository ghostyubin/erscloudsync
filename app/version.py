"""Single source of truth for the build stamp shown in the UI.

Format: ``DEV_YYYYMMDDHHMM`` — date and local time of the build.

Bump it on **every** refactor / image rebuild so it is always obvious which
build a NAS is actually running:

    python3 bump_version.py

``./build.sh`` does this automatically before building the image.
"""

APP_VERSION = "DEV_202610261601"
