# MeshView Docker Container

This Dockerfile builds a containerized version of the [MeshView](https://github.com/pablorevilla-meshtastic/meshview) application. It uses a lightweight Python environment and sets up the required virtual environment as expected by the application.

## Image Details

- **Base Image**: `python:3.12-slim`
- **Working Directory**: `/app`
- **Python Virtual Environment**: `/app/env`
- **Exposed Port**: `8081`

## Build Instructions

Build the Docker image:

```bash
docker build -t meshview-docker .
```

## Run Instructions

Run the container:

```bash
docker run -d --name meshview-docker -p 8081:8081 meshview-docker
```

This maps container port `8081` to your host. The application runs via:

```bash
/app/env/bin/python /app/mvrun.py
```

## Web Interface

Once the container is running, you can access the MeshView web interface by visiting:

http://localhost:8081

If running on a remote server, replace `localhost` with the host's IP or domain name:

http://<host>:8081

Ensure that port `8081` is open and not blocked by a firewall or security group.
