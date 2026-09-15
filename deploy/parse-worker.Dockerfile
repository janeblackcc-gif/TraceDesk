ARG APP_IMAGE
FROM docker:29.8.0-cli@sha256:eccaacfeed644c7de222ff047483568cb988dde95476fbaaf10ea2d04921bb66 AS docker_cli
FROM ${APP_IMAGE}
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
