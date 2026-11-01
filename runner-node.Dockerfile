FROM node:22-alpine
RUN sed -i 's/dl-cdn.alpinelinux.org/dl-2.alpinelinux.org/g' /etc/apk/repositories \
    && apk add --no-cache make bash python3 g++ \
    && corepack enable \
    && corepack prepare pnpm@10.12.1 --activate \
    && mkdir -p /workspace \
    && chown 1000:1000 /workspace
RUN mkdir -p /opt/corepack \
    && cp -R /root/.cache/node/corepack/. /opt/corepack/ \
    && chmod -R a+rX /opt/corepack
ENV HOME=/tmp/home CI=true HUSKY=0 COREPACK_ENABLE_PROJECT_SPEC=0 COREPACK_HOME=/opt/corepack COREPACK_ENABLE_NETWORK=0
WORKDIR /workspace
USER 1000:1000
