# syntax=docker/dockerfile:1
#
# Prebuild the CX-patched standalone Cycles binary.
# This removes the 5-6 minute per-run build tax observed in spec-lab.

FROM runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404 AS build

ARG CYCLES_REF=main
ARG PARALLEL_JOBS=8
ARG CYCLES_CUDA_ARCH=sm_89
ARG BUILD_CMAKE_ARGS="-DWITH_CYCLES_DEVICE_OPTIX=OFF -DWITH_CYCLES_CUDA_BINARIES=ON"

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update >/dev/null 2>&1 && \
    apt-get install -y \
      build-essential git git-lfs subversion cmake ninja-build pkg-config \
      python3 python3-dev make \
      libx11-dev libxxf86vm-dev libxcursor-dev libxi-dev libxrandr-dev \
      libxinerama-dev libegl-dev libwayland-dev wayland-protocols \
      libxkbcommon-dev libdbus-1-dev libepoxy-dev \
      libgl-dev libglu1-mesa-dev libglew-dev libsdl2-dev \
      curl ca-certificates file time openimageio-tools \
      >/dev/null 2>&1 && \
    git lfs install >/dev/null 2>&1

WORKDIR /opt

RUN git clone --filter=blob:none https://projects.blender.org/blender/cycles.git cx-cycles

WORKDIR /opt/cx-cycles

RUN git fetch --filter=blob:none origin "${CYCLES_REF}" && \
    git checkout --detach FETCH_HEAD && \
    git lfs install --local >/dev/null 2>&1

RUN make update

COPY patches/cycles/*.patch /tmp/cx-cycles-patches/

RUN for p in /tmp/cx-cycles-patches/*.patch; do \
      git apply --check "$p"; \
      git apply "$p"; \
      echo "CX_CYCLES_PATCH_APPLIED=$(basename "$p")"; \
    done

RUN BUILD_CMAKE_ARGS="${BUILD_CMAKE_ARGS} -DCYCLES_CUDA_BINARIES_ARCH=${CYCLES_CUDA_ARCH}" \
    PARALLEL_JOBS="${PARALLEL_JOBS}" \
    make

RUN ./install/cycles --help | grep -E -- '--sample-subset-offset|--sample-subset-length|--disable-adaptive-sampling' && \
    ./install/cycles --list-devices || true && \
    ./install/cycles --samples 8 --sample-subset-offset 0 --sample-subset-length 8 \
      --disable-adaptive-sampling \
      --output /tmp/cx_cycles_subset_monkey.png examples/scene_monkey.xml && \
    test -s /tmp/cx_cycles_subset_monkey.png

FROM runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404

ENV DEBIAN_FRONTEND=noninteractive
ENV PATH="/opt/cx-cycles/install:${PATH}"

RUN apt-get update >/dev/null 2>&1 && \
    apt-get install -y \
      libx11-6 libxxf86vm1 libxcursor1 libxi6 libxrandr2 libxinerama1 \
      libegl1 libwayland-client0 libxkbcommon0 libdbus-1-3 libepoxy0 \
      libgl1 libglu1-mesa libglew2.2 libsdl2-2.0-0 file time openimageio-tools \
      >/dev/null 2>&1

COPY --from=build /opt/cx-cycles /opt/cx-cycles

WORKDIR /opt/cx-cycles

CMD ["cycles", "--help"]
