#!/bin/bash
# BauduSync - Multi-arch Docker Build Script
# Builds for both linux/amd64 (x86) and linux/arm64 (RK3576/ARM)
#
# Usage:
#   ./build.sh              # Build for both architectures
#   ./build.sh amd64        # Build for x86 only
#   ./build.sh arm64        # Build for ARM only
#   ./build.sh push         # Build and push to registry

set -e

IMAGE_NAME="baudusync"
TAG="latest"
REGISTRY="${DOCKER_REGISTRY:-}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}BauduSync Docker Build Script${NC}"
echo "================================"

# Check if buildx is available
if ! docker buildx version &>/dev/null; then
    echo -e "${RED}Error: docker buildx is not available${NC}"
    echo "Please install buildx: https://github.com/docker/buildx"
    exit 1
fi

# Create/ensure builder exists
BUILDER_NAME="baudusync-builder"
if ! docker buildx inspect $BUILDER_NAME &>/dev/null; then
    echo -e "${YELLOW}Creating buildx builder...${NC}"
    docker buildx create --name $BUILDER_NAME --use
else
    docker buildx use $BUILDER_NAME
fi

# Determine platforms
TARGET="${1:-all}"
PLATFORMS=""

case "$TARGET" in
    amd64)
        PLATFORMS="linux/amd64"
        echo -e "${YELLOW}Building for x86_64 (amd64) only${NC}"
        ;;
    arm64)
        PLATFORMS="linux/arm64"
        echo -e "${YELLOW}Building for ARM64 (arm64) only${NC}"
        ;;
    push)
        if [ -z "$REGISTRY" ]; then
            echo -e "${RED}Error: DOCKER_REGISTRY not set${NC}"
            echo "Set it with: export DOCKER_REGISTRY=your-registry.com/username"
            exit 1
        fi
        PLATFORMS="linux/amd64,linux/arm64"
        echo -e "${YELLOW}Building and pushing for amd64 + arm64${NC}"
        ;;
    all|"")
        PLATFORMS="linux/amd64,linux/arm64"
        echo -e "${YELLOW}Building for amd64 + arm64${NC}"
        ;;
    *)
        echo -e "${RED}Unknown target: $TARGET${NC}"
        echo "Usage: $0 [amd64|arm64|push|all]"
        exit 1
        ;;
esac

# Full image name
if [ -n "$REGISTRY" ]; then
    FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:${TAG}"
else
    FULL_IMAGE="${IMAGE_NAME}:${TAG}"
fi

echo ""
echo "Image: $FULL_IMAGE"
echo "Platforms: $PLATFORMS"
echo ""

# Build
if [ "$TARGET" == "push" ]; then
    echo -e "${GREEN}Building and pushing...${NC}"
    docker buildx build \
        --platform "$PLATFORMS" \
        -t "$FULL_IMAGE" \
        --push \
        .
else
    echo -e "${GREEN}Building locally...${NC}"

    # For local multi-arch builds, we need to use --output type=docker
    # but that only works for single platform.
    # For multi-platform, we'll build each separately and load.

    if echo "$PLATFORMS" | grep -q ","; then
        # Multi-platform: build each and save as tar
        for PLATFORM in $(echo "$PLATFORMS" | tr ',' ' '); do
            ARCH=$(echo "$PLATFORM" | cut -d'/' -f2)
            echo -e "${YELLOW}Building $PLATFORM...${NC}"
            docker buildx build \
                --platform "$PLATFORM" \
                --build-arg CACHE_BUST=$(date +%s) \
                -t "${IMAGE_NAME}:${TAG}-${ARCH}" \
                --load \
                .
            echo -e "${GREEN}Built ${IMAGE_NAME}:${TAG}-${ARCH}${NC}"
        done

        # Also create a manifest
        echo -e "${YELLOW}Creating manifest...${NC}"
        docker manifest create "${IMAGE_NAME}:${TAG}" \
            "${IMAGE_NAME}:${TAG}-amd64" \
            "${IMAGE_NAME}:${TAG}-arm64" 2>/dev/null || true
        echo -e "${GREEN}Done! Images created:${NC}"
        echo "  - ${IMAGE_NAME}:${TAG}-amd64"
        echo "  - ${IMAGE_NAME}:${TAG}-arm64"
    else
        # Single platform: load directly
        docker buildx build \
            --platform "$PLATFORMS" \
            --build-arg CACHE_BUST=$(date +%s) \
            -t "$FULL_IMAGE" \
            --load \
            .
        echo -e "${GREEN}Built $FULL_IMAGE${NC}"
    fi
fi

echo ""
echo -e "${GREEN}Build complete!${NC}"
echo ""
echo "To run:"
echo "  docker run -d --name baudusync -p 8099:8099 -v baudusync-data:/app/data -v /path/to/nas/folder:/sync ${IMAGE_NAME}:${TAG}"
echo ""
echo "Or with docker-compose:"
echo "  docker-compose up -d"
