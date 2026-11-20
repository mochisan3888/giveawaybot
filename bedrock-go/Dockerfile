FROM golang:1.27.3-bookworm AS build
WORKDIR /src
ENV CGO_ENABLED=0 GOFLAGS=-p=2 GOMEMLIMIT=256MiB
COPY go.mod go.sum ./
RUN go mod download
COPY main.go main_test.go ./
RUN go test ./... && go build -trimpath -ldflags="-s -w" -o /verifier .
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /verifier /verifier
ENV GOMEMLIMIT=256MiB GOMAXPROCS=2
ENTRYPOINT ["/verifier"]
