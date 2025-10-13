#!/bin/bash
docker build -t tbfs-backend -f server/dockerfile.yml .
docker build -t tbfs-frontend -f gui/dockerfile.yml .
docker swarm init
docker stack deploy -c docker-compose.yml tbfs 