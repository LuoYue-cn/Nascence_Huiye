# Docker deployment

## Quick start

Requirements: Docker Engine and the Docker Compose plugin. After publishing the `source` directory as a GitHub repository, users can deploy it like this (replace the URL with the repository URL):

```bash
git clone <your-github-repository-url>
cd <cloned-repository-directory>
cp docker/.env.example docker/.env
```

Edit `docker/.env` and set two private secrets. The admin password must be at least 12 characters; the NapCat token must be at least 16 characters. Do not commit `.env`.

Build and start:

```bash
docker compose --env-file docker/.env -f docker/compose.yaml up -d --build
```

Check status/logs and open the admin panel:

```bash
docker compose --env-file docker/.env -f docker/compose.yaml ps
docker compose --env-file docker/.env -f docker/compose.yaml logs -f huiye
```

Compose publishes host port 8000 to container port 8000 (`8000:8000`). Open the panel at `http://<server-ip>:8000/`. To use a different host port, edit the left-hand side of the `ports` mapping in `docker/compose.yaml`; for example, `10002:8000` publishes host port 10002 while keeping the app's container port at 8000. For internet-facing deployments, restrict access with a firewall or HTTPS reverse proxy; do not proxy `/internal/*`.

NapCat's reverse WebSocket URL must use the host port:

```text
ws://<server-ip>:8000/internal/onebot/ws
```

Configure the same `HUIYE_NAPCAT_TOKEN` in NapCat. Ensure the bot's configured QQ number and group whitelist are correct in the admin panel.

## Ollama on the Docker host

The container can address a host Ollama service as `http://host.docker.internal:11434`; this hostname is added by Compose. On Linux, Ollama must listen on a host interface reachable from Docker (commonly set `OLLAMA_HOST=0.0.0.0:11434` in the host Ollama service). Configure the Ollama URL, embedding model and its actual vector dimension in the admin panel. Do not expose Ollama's port to the public internet.

## Updating and data persistence

After pulling a new source revision, rebuild and recreate the app:

```bash
docker compose --env-file docker/.env -f docker/compose.yaml up -d --build
```

Application data and runtime configuration persist in the named volumes `huiye-data` and `huiye-config`; removing/recreating the container does not remove them. **Do not run `docker compose down -v` unless you intend to delete those volumes and all persisted bot data/configuration.**

To stop the service while retaining data:

```bash
docker compose --env-file docker/.env -f docker/compose.yaml down
```

The compose file builds from the checked-out source. No pre-published Docker image is required.
