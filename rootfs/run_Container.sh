#!/bin/bash


echo "Running RootFS-Builder Container !!!"
sudo docker run --rm --privileged -v "$(pwd)/output:/app/build" docker.io/blackleakzde/rootfs-maker:latest
