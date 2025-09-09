#!/bin/bash

# FreeSky Deployment Script
# Handles different deployment scenarios

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to get host IP
get_host_ip() {
    local ip
    # Try different methods to get the host IP
    if command -v ip >/dev/null 2>&1; then
        ip=$(ip route get 1.1.1.1 | awk '{print $7; exit}' 2>/dev/null)
    elif command -v hostname >/dev/null 2>&1; then
        ip=$(hostname -I | awk '{print $1}' 2>/dev/null)
    fi
    
    if [ -n "$ip" ] && [ "$ip" != "127.0.0.1" ]; then
        echo "$ip"
    else
        echo "localhost"
    fi
}

# Function to check if container exists
container_exists() {
    docker ps -a --format "table {{.Names}}" | grep -q "^$1\$" 2>/dev/null
}

# Function to deploy standalone
deploy_standalone() {
    print_info "Deploying FreeSky in standalone mode..."
    
    # Get host IP
    HOST_IP=$(get_host_ip)
    print_info "Detected host IP: $HOST_IP"
    
    # Create .env file if it doesn't exist
    if [ ! -f .env ]; then
        print_info "Creating .env file from template..."
        cp .env.example .env
        sed -i "s/DOCKER_HOST_IP=192.168.1.100/DOCKER_HOST_IP=$HOST_IP/g" .env
        sed -i "s/API_URL=http:\/\/192.168.1.100:3001/API_URL=http:\/\/$HOST_IP:3001/g" .env
    fi
    
    # Deploy using standalone compose file
    docker-compose -f docker-compose.standalone.yml down 2>/dev/null || true
    docker-compose -f docker-compose.standalone.yml up -d --build
    
    # Wait for container to start
    print_info "Waiting for container to start..."
    sleep 10
    
    # Check health
    if curl -sf "http://$HOST_IP:3001/health" >/dev/null 2>&1; then
        print_success "FreeSky deployed successfully!"
        print_info "Access URLs:"
        print_info "  Frontend: http://$HOST_IP:3001"
        print_info "  API: http://$HOST_IP:3001/api/"
        print_info "  Channels: http://$HOST_IP:3001/channels"
        print_info "  Playlist: http://$HOST_IP:3001/playlist.m3u8"
    else
        print_error "Health check failed. Check logs with: docker-compose -f docker-compose.standalone.yml logs"
    fi
}

# Function to deploy with transmission
deploy_with_transmission() {
    print_info "Deploying FreeSky with transmission-openvpn integration..."
    
    # Check if transmission-openvpn container exists
    if ! container_exists "transmission-openvpn"; then
        print_error "transmission-openvpn container not found!"
        print_info "Please start your transmission-openvpn container first, or use standalone deployment."
        exit 1
    fi
    
    # Get transmission container IP
    TRANSMISSION_IP=$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' transmission-openvpn)
    print_info "Transmission container IP: $TRANSMISSION_IP"
    
    # Create .env file if it doesn't exist
    if [ ! -f .env ]; then
        print_info "Creating .env file for transmission deployment..."
        cp .env.example .env
        sed -i "s/DOCKER_HOST_IP=192.168.1.100/DOCKER_HOST_IP=$TRANSMISSION_IP/g" .env
        sed -i "s/API_URL=http:\/\/192.168.1.100:3001/API_URL=http:\/\/$TRANSMISSION_IP:3001/g" .env
    fi
    
    # Deploy using main compose file
    docker-compose down 2>/dev/null || true
    docker-compose up -d --build
    
    # Wait for container to start
    print_info "Waiting for container to start..."
    sleep 10
    
    print_success "FreeSky deployed with transmission-openvpn integration!"
    print_info "Access through transmission-openvpn container's network interface"
}

# Function to show usage
show_usage() {
    cat << EOF
FreeSky Deployment Script

Usage: $0 [OPTION]

Options:
    standalone          Deploy in standalone mode (recommended for Portainer)
    transmission        Deploy with transmission-openvpn integration
    status             Show deployment status
    logs               Show container logs
    stop               Stop the deployment
    help               Show this help message

Examples:
    $0 standalone       # Deploy for Portainer or direct Docker access
    $0 transmission     # Deploy with transmission-openvpn
    $0 status          # Check if containers are running
    $0 logs            # Show recent logs

EOF
}

# Function to show status
show_status() {
    print_info "Checking FreeSky deployment status..."
    
    if docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -q "freesky"; then
        print_success "FreeSky container is running:"
        docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep "freesky"
    else
        print_warning "No FreeSky containers are currently running"
    fi
}

# Function to show logs
show_logs() {
    if docker ps --format "table {{.Names}}" | grep -q "freesky"; then
        print_info "Showing FreeSky logs (last 50 lines)..."
        if [ -f "docker-compose.standalone.yml" ] && docker-compose -f docker-compose.standalone.yml ps | grep -q "freesky"; then
            docker-compose -f docker-compose.standalone.yml logs --tail=50 -f
        else
            docker-compose logs --tail=50 -f
        fi
    else
        print_error "No FreeSky containers are running"
    fi
}

# Function to stop deployment
stop_deployment() {
    print_info "Stopping FreeSky deployment..."
    
    # Try both compose files
    docker-compose -f docker-compose.standalone.yml down 2>/dev/null || true
    docker-compose down 2>/dev/null || true
    
    print_success "FreeSky deployment stopped"
}

# Main script logic
case "${1:-}" in
    "standalone")
        deploy_standalone
        ;;
    "transmission")
        deploy_with_transmission
        ;;
    "status")
        show_status
        ;;
    "logs")
        show_logs
        ;;
    "stop")
        stop_deployment
        ;;
    "help"|"--help"|"-h")
        show_usage
        ;;
    "")
        print_info "No deployment mode specified. Choose one:"
        echo
        show_usage
        ;;
    *)
        print_error "Unknown option: $1"
        echo
        show_usage
        exit 1
        ;;
esac