# Minimal docker-out-of-docker smoke test.
# If a box (running as the non-root `ctf` agent, with the host socket bound in) can
# `docker build` and `docker run` this, then the bot's agents can spin up service
# containers. Tests BuildKit (the RUN step) + the socket + sibling-container run.
#   docker build -t doodtest .
#   docker run --rm doodtest
FROM alpine:3.20
RUN echo "built $(date -u +%H:%M:%S) via BuildKit" > /built.txt
CMD ["sh", "-c", "echo 'DooD OK - agent built AND ran a sibling container'; cat /built.txt; id"]
