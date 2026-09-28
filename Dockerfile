FROM python:3.13-slim-trixie AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git ninja-build pkg-config ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY CMakeLists.txt ./
COPY native ./native
RUN cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_LTO=OFF \
    && cmake --build build --parallel 2

FROM python:3.13-slim-trixie
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY gba_link ./gba_link
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home gba-link \
    && mkdir /data && chown gba-link:gba-link /data
COPY --from=build /src/build/libgba_link.so /app/libgba_link.so
COPY --from=build /src/build/_deps/mgba-src/LICENSE /usr/share/licenses/mgba/LICENSE
ENV GBA_LINK_LIBRARY=/app/libgba_link.so DATA_ROOT=/data ROM_ROOT=/roms
USER gba-link
EXPOSE 8080
CMD ["python", "-m", "gba_link.server"]
