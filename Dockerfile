# Build instructions:
# docker build . -f Dockerfile -t ot:tag
# docker run -it --rm -v `cwd`:/usr/local/workspace ot:tag
FROM python:3.8-slim-buster as builder

RUN apt-get update
RUN apt-get install -y --no-install-recommends \
        cmake build-essential wget ca-certificates unzip pkg-config \
        zlib1g-dev libfreexl-dev libxml2-dev

# making directory to avoid this JDK installation bug: https://github.com/geerlingguy/ansible-role-java/issues/64
RUN mkdir /usr/share/man/man1
RUN apt-get install -y openjdk-11-jdk-headless

RUN apt-get install -y \
    emacs \
    gcc \
    git \
    libffi-dev \
    openjdk-11-jdk \
    unzip

WORKDIR /tmp

# using gdal master
ENV CPUS 4
ENV CURL_VERSION 7.61.1
ENV GDAL_VERSION 3.0.4
ENV GEOS_VERSION 3.8.0
ENV OPENJPEG_VERSION 2.3.1
ENV PROJ_VERSION 7.0.0
ENV SPATIALITE_VERSION 4.3.0a
ENV SQLITE_VERSION 3270200
ENV WEBP_VERSION 1.0.0
ENV ZSTD_VERSION 1.3.4
ENV TIFF_VERSION 4.1.0
ENV GEOTIFF_VERSION 1.5.1

RUN wget -q https://storage.googleapis.com/downloads.webmproject.org/releases/webp/libwebp-${WEBP_VERSION}.tar.gz
RUN wget -q -O zstd-${ZSTD_VERSION}.tar.gz https://github.com/facebook/zstd/archive/v${ZSTD_VERSION}.tar.gz
RUN wget -q https://download.osgeo.org/geos/geos-${GEOS_VERSION}.tar.bz2
RUN wget -q https://download.osgeo.org/proj/proj-${PROJ_VERSION}.tar.gz
RUN wget -q https://curl.haxx.se/download/curl-${CURL_VERSION}.tar.gz
RUN wget -q -O openjpeg-${OPENJPEG_VERSION}.tar.gz https://github.com/uclouvain/openjpeg/archive/v${OPENJPEG_VERSION}.tar.gz
RUN wget -q https://download.osgeo.org/gdal/${GDAL_VERSION}/gdal-${GDAL_VERSION}.tar.gz
RUN wget -q https://www.sqlite.org/2019/sqlite-autoconf-${SQLITE_VERSION}.tar.gz
#           https://www.sqlite.org/2019/sqlite-autoconf-3270200.tar.gz
RUN wget -q https://www.gaia-gis.it/gaia-sins/libspatialite-${SPATIALITE_VERSION}.tar.gz
RUN wget -q https://download.osgeo.org/proj/proj-datumgrid-1.8.zip

RUN tar xzf libwebp-${WEBP_VERSION}.tar.gz && \
    cd libwebp-${WEBP_VERSION} && \
    CFLAGS="-O2 -Wl,-S" ./configure --enable-silent-rules && \
    echo "building WEBP ${WEBP_VERSION}..." \
    make --quiet -j${CPUS} && make --quiet install

RUN tar -zxf zstd-${ZSTD_VERSION}.tar.gz \
    && cd zstd-${ZSTD_VERSION} \
    && echo "building ZSTD ${ZSTD_VERSION}..." \
    && make --quiet -j${CPUS} ZSTD_LEGACY_SUPPORT=0 CFLAGS=-O1 \
    && make --quiet install ZSTD_LEGACY_SUPPORT=0 CFLAGS=-O1

RUN tar -xjf geos-${GEOS_VERSION}.tar.bz2 \
    && cd geos-${GEOS_VERSION} \
    && ./configure --prefix=/usr/local \
    && echo "building geos ${GEOS_VERSION}..." \
    && make --quiet -j${CPUS} && make --quiet install

RUN tar -xzvf sqlite-autoconf-${SQLITE_VERSION}.tar.gz && cd sqlite-autoconf-${SQLITE_VERSION} \
    && ./configure --prefix=/usr/local \
    && echo "building SQLITE ${SQLITE_VERSION}..." \
    && make --quiet -j${CPUS} && make --quiet install

RUN wget -q https://download.osgeo.org/libtiff/tiff-${TIFF_VERSION}.tar.gz \
    && tar -xf tiff-${TIFF_VERSION}.tar.gz \
    && cd tiff-${TIFF_VERSION} \
    && ./configure \
    && make -j ${CPUS} && make install


RUN tar -xzf curl-${CURL_VERSION}.tar.gz && cd curl-${CURL_VERSION} \
    && ./configure --prefix=/usr/local \
    && echo "building CURL ${CURL_VERSION}..." \
    && make --quiet -j${CPUS} && make --quiet install


RUN tar -xzf proj-${PROJ_VERSION}.tar.gz \
    && unzip proj-datumgrid-1.8.zip -d proj-${PROJ_VERSION}/data \
    && cd proj-${PROJ_VERSION} \
    && ./configure --prefix=/usr/local \
    && echo "building proj ${PROJ_VERSION}..." \
    && make --quiet -j${CPUS} && make --quiet install

# Doesn't appear to be updated for proj6, not worth holding up the show
# RUN tar -xzvf libspatialite-${SPATIALITE_VERSION}.tar.gz && cd libspatialite-${SPATIALITE_VERSION} \
#     && ./configure --prefix=/usr/local \
#     && echo "building SPATIALITE ${SPATIALITE_VERSION}..." \
#     && make --quiet -j${CPUS} && make --quiet install

RUN wget -q http://download.osgeo.org/geotiff/libgeotiff/libgeotiff-${GEOTIFF_VERSION}.tar.gz \
    && tar -xf libgeotiff-${GEOTIFF_VERSION}.tar.gz \
    && cd libgeotiff-${GEOTIFF_VERSION} \
    && ./configure \
    && make -j ${CPUS} && make install

RUN tar -zxf openjpeg-${OPENJPEG_VERSION}.tar.gz \
    && cd openjpeg-${OPENJPEG_VERSION} \
    && mkdir build && cd build \
    && cmake .. -DBUILD_THIRDPARTY:BOOL=ON -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
    && echo "building openjpeg ${OPENJPEG_VERSION}..." \
    && make --quiet -j${CPUS} && make --quiet install

RUN tar -xzf gdal-${GDAL_VERSION}.tar.gz && cd gdal-${GDAL_VERSION} && \
    ./configure \
        --disable-debug \
        --disable-static \
        --prefix=/usr/local \
        --with-curl=/usr/local/bin/curl-config \
        --with-geos \
        --with-geotiff=internal \
        --with-hide-internal-symbols=yes \
        --with-libtiff=internal \
        --with-python \
        --with-openjpeg \
        --with-sqlite3 \
        --with-spatialite \
        --with-proj=/usr/local \
        --with-rename-internal-libgeotiff-symbols=yes \
        --with-rename-internal-libtiff-symbols=yes \
        --with-threads=yes \
        --with-webp=/usr/local \
        --with-zstd=/usr/local \
    && echo "building GDAL ${GDAL_VERSION}..." \
    && make --quiet -j${CPUS} && make --quiet install

RUN ldconfig

RUN apt update && apt upgrade
RUN apt install libspatialindex-dev -y

RUN pip3 install --no-cache-dir \
    Cython \
    flask \
    matplotlib \
    numpy \
    requests \
    retrying \
    rtree \
    scipy \
    shapely \
    git+https://github.com/natcap/pygeoprocessing.git@release/2.0

RUN apt install -y \
    openssl \
    curl
SHELL ["/bin/bash", "-c"]
WORKDIR /usr/local/gcloud-sdk
RUN wget https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-sdk-284.0.0-linux-x86_64.tar.gz && tar -xvzf google-cloud-sdk-284.0.0-linux-x86_64.tar.gz
RUN ./google-cloud-sdk/install.sh
RUN source /usr/local/gcloud-sdk/google-cloud-sdk/completion.bash.inc
RUN source /usr/local/gcloud-sdk/google-cloud-sdk/path.bash.inc
RUN echo "export PATH=$PATH:/usr/local/gcloud-sdk/google-cloud-sdk/bin" >> /root/.bashrc

COPY salo-api-5de978810708.json /usr/local/salo-api-5de978810708.json
RUN /usr/local/gcloud-sdk/google-cloud-sdk/bin/gcloud auth activate-service-account --key-file=/usr/local/salo-api-5de978810708.json
RUN /usr/local/gcloud-sdk/google-cloud-sdk/bin/gcloud config set project natgeo-dams
RUN rm /usr/local/salo-api-5de978810708.json

WORKDIR /usr/local
COPY geoserver-2.16.2-bin.zip .
RUN unzip geoserver-2.16.2-bin.zip
RUN rm geoserver-2.16.2-bin.zip
RUN mv ./geoserver-2.16.2 ./geoserver

COPY start_geoserver.sh /usr/local/geoserver/bin
COPY geoserver_flask_manager.py /usr/local/geoserver/bin
COPY geoserver_tracer.py /usr/local/geoserver/bin

RUN pip3 install ecoshard==0.4.0

WORKDIR /usr/local/workspace
ENTRYPOINT ["bash"]
