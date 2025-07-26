# FreeSky - Live TV Streaming Application

A modern web application for streaming live TV channels with a beautiful, responsive interface built with Reflex (React + Python).

## 🚀 Quick Start

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd freeskyNew
   ```

2. **Set up environment**
   ```bash
   cp env.example .env
   # Edit .env with your configuration
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Run the application**
   ```bash
   python -m freesky.backend_app
   ```

5. **Access the application**
   - Open your browser and navigate to `http://localhost:3000`
   - The application will automatically load available channels

## 📚 Documentation

This project includes comprehensive documentation organized in the `documentation/` folder:

### 📖 [Main Documentation](documentation/README.md)
Complete project overview, features, and detailed setup instructions.

### 🏗️ [Streaming Architecture](documentation/STREAMING_ARCHITECTURE.md)
Technical deep-dive into how the application acquires and processes upstream streaming links.

### 🔗 [Multi-Service Integration](documentation/MULTI_SERVICE_INTEGRATION.md)
Guide to integrating multiple upstream streaming services, inspired by Kodi addons.

### 🚀 [Deployment Guide](documentation/DEPLOYMENT.md)
Step-by-step instructions for deploying the application in various environments.

### 🔒 [Security Documentation](documentation/SECURITY.md)
Security considerations, best practices, and implementation details.

### 🔧 [Troubleshooting Guide](documentation/TROUBLESHOOTING.md)
Common issues, solutions, and debugging tips.

## 🎯 Features

- **Multi-Service Streaming**: Support for multiple upstream streaming services with automatic fallback
- **Live TV Streaming**: Access to hundreds of live TV channels
- **Modern UI**: Beautiful, responsive interface built with Reflex
- **Channel Management**: Browse, search, and organize channels across all services
- **Service Management**: Enable/disable streaming services dynamically
- **Playlist Support**: Generate M3U8 playlists for external players
- **Schedule Information**: View program schedules for channels
- **Proxy Support**: Built-in SOCKS5 proxy support
- **Docker Ready**: Containerized deployment with Docker

## 🛠️ Technology Stack

- **Backend**: Python with Reflex framework
- **Frontend**: React components with Reflex
- **Streaming**: HLS (HTTP Live Streaming) support
- **Proxy**: curl_cffi for advanced HTTP requests
- **Deployment**: Docker and Docker Compose
- **Web Server**: Caddy for production deployment

## 📁 Project Structure

```
freeskyNew/
├── documentation/          # All documentation files
├── freesky/               # Main application code
│   ├── components/        # UI components
│   ├── pages/            # Application pages
│   └── backend_app.py    # Main application entry point
├── assets/               # Static assets (images, icons)
├── docker-compose.yml    # Docker deployment configuration
├── Dockerfile           # Docker container definition
├── requirements.txt     # Python dependencies
└── README.md           # This file
```

## 🔧 Configuration

The application uses several configuration options:

- **Environment Variables**: See `env.example` for available options
- **SOCKS5 Proxy**: Configure proxy settings for network access
- **Stream Limits**: Control concurrent stream connections
- **API Endpoints**: Customize API URLs and endpoints

## 🐳 Docker Deployment

Quick deployment with Docker:

```bash
docker-compose up -d
```

See the [Deployment Guide](documentation/DEPLOYMENT.md) for detailed instructions.

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## ⚠️ Disclaimer

This application is for educational and personal use only. Users are responsible for ensuring they have the right to access any content streamed through this application.

## 🆘 Support

- **Documentation**: Check the [documentation folder](documentation/) for detailed guides
- **Troubleshooting**: See [Troubleshooting Guide](documentation/TROUBLESHOOTING.md) for common issues
- **Issues**: Report bugs and feature requests through the project's issue tracker

---

**FreeSky** - Bringing live TV streaming to the modern web. 