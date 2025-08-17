# CLAUDE.md - FreeSky Development Guide & Documentation Standards

## 📋 Table of Contents

1. [Project Overview](#project-overview)
2. [Architecture & Technology Stack](#architecture--technology-stack)
3. [Development Standards](#development-standards)
4. [Documentation Requirements](#documentation-requirements)
5. [Best Practices](#best-practices)
6. [Code Quality Standards](#code-quality-standards)
7. [Testing Guidelines](#testing-guidelines)
8. [Deployment & DevOps](#deployment--devops)
9. [Security Guidelines](#security-guidelines)
10. [Troubleshooting & Support](#troubleshooting--support)

---

## 🎯 Project Overview

**FreeSky** is a self-hosted IPTV proxy application built with Reflex (React + Python) that enables streaming of over 1,000 live TV channels. The application provides a modern web interface for browsing channels, searching for live events, and generating M3U8 playlists for external media players.

### Key Features
- Multi-service streaming with automatic fallback
- Live TV streaming with HLS support
- Modern responsive UI built with Reflex
- Channel management and search functionality
- Playlist generation for external players
- Docker-ready deployment
- SOCKS5 proxy support

### Project Structure
```
freeskyNew/
├── documentation/          # Comprehensive documentation
├── freesky/               # Main application code
│   ├── components/        # UI components
│   ├── pages/            # Application pages
│   ├── test/             # Test files and debugging tools
│   └── backend_app.py    # Main application entry point
├── assets/               # Static assets
├── docker-compose.yml    # Docker deployment configuration
├── Dockerfile           # Container definition
└── requirements.txt     # Python dependencies
```

---

## 🏗️ Architecture & Technology Stack

### Core Technologies
- **Backend**: Python with Reflex framework (v0.8.0)
- **Frontend**: React components via Reflex compilation
- **Streaming**: HLS (HTTP Live Streaming) support
- **HTTP Client**: curl_cffi (v0.11.4) for advanced requests
- **Server**: Uvicorn with FastAPI for API endpoints
- **Deployment**: Docker and Docker Compose
- **Web Server**: Caddy for production deployment

### Architecture Components
- **Multi-Service Integration**: Support for multiple upstream streaming services
- **Stream Monitoring**: Real-time stream health monitoring
- **Proxy Layer**: Content proxying with SOCKS5 support
- **API Layer**: RESTful endpoints for channel and stream management

### Documentation References
- **Main Architecture**: [documentation/README.md](documentation/README.md)
- **Streaming Architecture**: [documentation/STREAMING_ARCHITECTURE.md](documentation/STREAMING_ARCHITECTURE.md)
- **Multi-Service Integration**: [documentation/MULTI_SERVICE_INTEGRATION.md](documentation/MULTI_SERVICE_INTEGRATION.md)
- **Deployment Guide**: [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md)
- **Docker Deployment**: [documentation/DOCKER_DEPLOYMENT.md](documentation/DOCKER_DEPLOYMENT.md)
- **Security Documentation**: [documentation/SECURITY.md](documentation/SECURITY.md)
- **Troubleshooting**: [documentation/TROUBLESHOOTING.md](documentation/TROUBLESHOOTING.md)

---

## 📝 Development Standards

### Code Organization
1. **Modular Structure**: Organize code into logical modules and packages
2. **Separation of Concerns**: Keep UI components, business logic, and data access separate
3. **Consistent Naming**: Use descriptive, consistent naming conventions
4. **Import Organization**: Group imports logically (standard library, third-party, local)

### File Naming Conventions
- **Python Files**: Use snake_case (e.g., `backend_app.py`, `stream_monitor.py`)
- **Component Files**: Use snake_case for Reflex components (e.g., `media_player.py`)
- **Test Files**: Prefix with `test_` (e.g., `test_backend.py`)
- **Configuration Files**: Use descriptive names (e.g., `docker-compose.yml`)

### Code Style
- Follow PEP 8 for Python code
- Use type hints where appropriate
- Include docstrings for all functions and classes
- Keep functions focused and single-purpose
- Maintain consistent indentation (4 spaces for Python)

---

## 📚 Documentation Requirements

### Mandatory Documentation Standards

#### 1. **Code Documentation**
- **REQUIRED**: All functions must have docstrings explaining purpose, parameters, and return values
- **REQUIRED**: All classes must have class-level docstrings
- **REQUIRED**: Complex logic must include inline comments
- **REQUIRED**: API endpoints must be documented with FastAPI decorators

#### 2. **Architecture Documentation**
- **REQUIRED**: Document all major architectural decisions
- **REQUIRED**: Update architecture diagrams when making significant changes
- **REQUIRED**: Document integration points with external services

#### 3. **Process Documentation**
- **REQUIRED**: Document deployment procedures
- **REQUIRED**: Document configuration options and environment variables
- **REQUIRED**: Document troubleshooting procedures for common issues

### Documentation Update Requirements

#### When Documentation MUST Be Updated:
1. **Adding New Features**: Create or update relevant documentation
2. **Changing APIs**: Update API documentation and integration guides
3. **Modifying Architecture**: Update architecture documentation
4. **Adding Dependencies**: Update requirements and dependency documentation
5. **Changing Configuration**: Update deployment and configuration guides
6. **Fixing Issues**: Update troubleshooting documentation with new solutions

#### Documentation Review Process:
1. **Code Review**: Documentation updates must be part of code reviews
2. **Architecture Review**: Major changes require architecture documentation review
3. **User Impact**: Changes affecting users require user-facing documentation updates

### Existing Documentation Structure
The project maintains comprehensive documentation in the `documentation/` directory:

- **README.md**: Main project overview and quick start guide
- **STREAMING_ARCHITECTURE.md**: Technical deep-dive into streaming implementation
- **MULTI_SERVICE_INTEGRATION.md**: Guide for integrating multiple streaming services
- **DEPLOYMENT.md**: Step-by-step deployment instructions
- **DOCKER_DEPLOYMENT.md**: Docker-specific deployment guide
- **SECURITY.md**: Security considerations and best practices
- **TROUBLESHOOTING.md**: Common issues and solutions

---

## ✅ Best Practices

### UI/UX Best Practices

#### 1. **Responsive Design**
- **REQUIRED**: All components must be mobile-responsive
- **REQUIRED**: Use Reflex's responsive utilities for layout
- **REQUIRED**: Test on multiple screen sizes and devices
- **BEST PRACTICE**: Implement progressive enhancement

#### 2. **User Experience**
- **REQUIRED**: Provide loading states for async operations
- **REQUIRED**: Implement error handling with user-friendly messages
- **REQUIRED**: Use consistent styling and theming
- **BEST PRACTICE**: Implement keyboard navigation support
- **BEST PRACTICE**: Provide accessibility features (ARIA labels, semantic HTML)

#### 3. **Performance**
- **REQUIRED**: Optimize component rendering and state updates
- **REQUIRED**: Implement lazy loading for large datasets
- **REQUIRED**: Use efficient data fetching patterns
- **BEST PRACTICE**: Implement caching strategies for frequently accessed data

#### 4. **Component Design**
- **REQUIRED**: Create reusable, composable components
- **REQUIRED**: Use props for component configuration
- **REQUIRED**: Implement proper state management
- **BEST PRACTICE**: Follow the single responsibility principle

### Backend Best Practices

#### 1. **API Design**
- **REQUIRED**: Use RESTful conventions for API endpoints
- **REQUIRED**: Implement proper HTTP status codes
- **REQUIRED**: Use consistent response formats
- **REQUIRED**: Implement request validation
- **BEST PRACTICE**: Version APIs appropriately

#### 2. **Error Handling**
- **REQUIRED**: Implement comprehensive error handling
- **REQUIRED**: Log errors with appropriate detail levels
- **REQUIRED**: Return meaningful error messages to clients
- **BEST PRACTICE**: Implement retry mechanisms for transient failures

#### 3. **Performance & Scalability**
- **REQUIRED**: Use async/await for I/O operations
- **REQUIRED**: Implement connection pooling for database/HTTP connections
- **REQUIRED**: Use efficient data structures and algorithms
- **BEST PRACTICE**: Implement caching where appropriate
- **BEST PRACTICE**: Monitor and optimize resource usage

#### 4. **Security**
- **REQUIRED**: Validate all user inputs
- **REQUIRED**: Implement proper authentication and authorization
- **REQUIRED**: Use HTTPS in production
- **REQUIRED**: Sanitize data before storage or output
- **BEST PRACTICE**: Implement rate limiting
- **BEST PRACTICE**: Use security headers

### Streaming-Specific Best Practices

#### 1. **Stream Management**
- **REQUIRED**: Implement proper stream lifecycle management
- **REQUIRED**: Handle stream failures gracefully
- **REQUIRED**: Implement fallback mechanisms
- **REQUIRED**: Monitor stream health and performance
- **BEST PRACTICE**: Implement adaptive bitrate streaming

#### 2. **Resource Management**
- **REQUIRED**: Limit concurrent stream connections
- **REQUIRED**: Implement proper cleanup of unused resources
- **REQUIRED**: Monitor memory and CPU usage
- **BEST PRACTICE**: Implement resource pooling

#### 3. **Proxy and Network**
- **REQUIRED**: Handle network timeouts appropriately
- **REQUIRED**: Implement retry logic for failed requests
- **REQUIRED**: Use connection pooling for HTTP requests
- **BEST PRACTICE**: Implement circuit breakers for external services

---

## 🔍 Code Quality Standards

### Code Review Requirements

#### 1. **Pre-Review Checklist**
- [ ] Code follows project style guidelines
- [ ] All functions have proper docstrings
- [ ] Type hints are used where appropriate
- [ ] Error handling is implemented
- [ ] Tests are written for new functionality
- [ ] Documentation is updated

#### 2. **Review Criteria**
- **Functionality**: Does the code work as intended?
- **Performance**: Is the code efficient and scalable?
- **Security**: Are there any security vulnerabilities?
- **Maintainability**: Is the code easy to understand and modify?
- **Testability**: Is the code testable and well-tested?

### Testing Standards

#### 1. **Test Coverage Requirements**
- **REQUIRED**: Unit tests for all business logic
- **REQUIRED**: Integration tests for API endpoints
- **REQUIRED**: Component tests for UI components
- **REQUIRED**: use PlayWright MCP server to run application in browser and observe all looks and works correctly as per expectation. Take screenshots of all relevant pages and make sure.
- **BEST PRACTICE**: End-to-end tests for critical user flows

#### 2. **Test Organization**
- **REQUIRED**: Tests must be in the `freesky/test/` directory
- **REQUIRED**: Test files must be prefixed with `test_`
- **REQUIRED**: Tests must be independent and repeatable
- **BEST PRACTICE**: Use descriptive test names

#### 3. **Test Data**
- **REQUIRED**: Use fixtures for test data
- **REQUIRED**: Clean up test data after tests
- **BEST PRACTICE**: Use factories for generating test objects

---

## 🚀 Deployment & DevOps

### Environment Configuration

#### 1. **Environment Variables**
- **REQUIRED**: Use environment variables for configuration
- **REQUIRED**: Provide default values for all configuration options
- **REQUIRED**: Document all environment variables
- **BEST PRACTICE**: Use a configuration management system

#### 2. **Docker Configuration**
- **REQUIRED**: Use multi-stage builds for optimal image size
- **REQUIRED**: Implement health checks
- **REQUIRED**: Use non-root users in containers
- **BEST PRACTICE**: Implement proper logging

### Deployment Process

#### 1. **Pre-Deployment Checklist**
- [ ] All tests pass
- [ ] Documentation is updated
- [ ] Environment variables are configured
- [ ] Security review is completed
- [ ] Performance testing is done

#### 2. **Deployment Steps**
1. Build Docker image
2. Run health checks
3. Deploy to staging environment
4. Run integration tests
5. Deploy to production
6. Monitor application health

### Monitoring and Logging

#### 1. **Logging Requirements**
- **REQUIRED**: Use structured logging
- **REQUIRED**: Log at appropriate levels (DEBUG, INFO, WARNING, ERROR)
- **REQUIRED**: Include context in log messages
- **BEST PRACTICE**: Use centralized logging

#### 2. **Monitoring Requirements**
- **REQUIRED**: Monitor application health
- **REQUIRED**: Monitor resource usage
- **REQUIRED**: Monitor error rates
- **BEST PRACTICE**: Set up alerting for critical issues

---

## 🔒 Security Guidelines

### Security Requirements

#### 1. **Input Validation**
- **REQUIRED**: Validate all user inputs
- **REQUIRED**: Sanitize data before processing
- **REQUIRED**: Use parameterized queries
- **BEST PRACTICE**: Implement input length limits

#### 2. **Authentication & Authorization**
- **REQUIRED**: Implement proper authentication
- **REQUIRED**: Use secure session management
- **REQUIRED**: Implement role-based access control
- **BEST PRACTICE**: Use OAuth 2.0 or similar standards

#### 3. **Data Protection**
- **REQUIRED**: Encrypt sensitive data in transit
- **REQUIRED**: Encrypt sensitive data at rest
- **REQUIRED**: Implement proper key management
- **BEST PRACTICE**: Use secure random number generation

### Security Best Practices

#### 1. **Network Security**
- **REQUIRED**: Use HTTPS in production
- **REQUIRED**: Implement proper CORS policies
- **REQUIRED**: Use security headers
- **BEST PRACTICE**: Implement rate limiting

#### 2. **Dependency Security**
- **REQUIRED**: Keep dependencies updated
- **REQUIRED**: Scan for known vulnerabilities
- **REQUIRED**: Use trusted package sources
- **BEST PRACTICE**: Implement automated security scanning

---

## 🛠️ Troubleshooting & Support

### Debugging Guidelines

#### 1. **Logging for Debugging**
- **REQUIRED**: Use appropriate log levels
- **REQUIRED**: Include relevant context in log messages
- **REQUIRED**: Use structured logging for complex data
- **BEST PRACTICE**: Implement request tracing

#### 2. **Error Handling**
- **REQUIRED**: Catch and handle all exceptions
- **REQUIRED**: Provide meaningful error messages
- **REQUIRED**: Log errors with stack traces
- **BEST PRACTICE**: Implement error reporting

### Support Process

#### 1. **Issue Reporting**
- **REQUIRED**: Use standardized issue templates
- **REQUIRED**: Include relevant logs and error messages
- **REQUIRED**: Provide steps to reproduce issues
- **BEST PRACTICE**: Include environment information

#### 2. **Issue Resolution**
- **REQUIRED**: Acknowledge issues promptly
- **REQUIRED**: Provide status updates
- **REQUIRED**: Document solutions in troubleshooting guide
- **BEST PRACTICE**: Implement automated issue tracking

---

## 📋 Documentation Checklist

### For New Features
- [ ] Update main README.md if feature affects user experience
- [ ] Create or update relevant architecture documentation
- [ ] Update API documentation if new endpoints are added
- [ ] Update deployment documentation if configuration changes
- [ ] Update troubleshooting guide if new issues might arise

### For Bug Fixes
- [ ] Update troubleshooting guide with the fix
- [ ] Document any workarounds or known issues
- [ ] Update relevant documentation if the fix changes behavior

### For Configuration Changes
- [ ] Update environment variable documentation
- [ ] Update deployment guides
- [ ] Update configuration examples
- [ ] Document any migration steps required

---

## 🔄 Maintenance and Updates

### Regular Maintenance Tasks
1. **Monthly**: Review and update dependencies
2. **Monthly**: Review and update documentation
3. **Quarterly**: Security audit and updates
4. **Quarterly**: Performance review and optimization
5. **Annually**: Architecture review and refactoring

### Documentation Maintenance
- **REQUIRED**: Keep documentation synchronized with code changes
- **REQUIRED**: Review documentation accuracy quarterly
- **REQUIRED**: Update examples and tutorials as needed
- **BEST PRACTICE**: Implement documentation versioning

---

## 📞 Contact and Resources

### Development Team
- **Primary Contact**: Development team lead
- **Documentation Maintainer**: Assigned team member
- **Security Contact**: Security team lead

### Resources
- **Project Repository**: [Repository URL]
- **Documentation**: `documentation/` directory
- **Issue Tracking**: [Issue Tracker URL]
- **CI/CD Pipeline**: [Pipeline URL]

### External References
- **Reflex Documentation**: https://reflex.dev/docs
- **FastAPI Documentation**: https://fastapi.tiangolo.com/
- **Docker Documentation**: https://docs.docker.com/
- **HLS Specification**: https://tools.ietf.org/html/rfc8216

---

*This document should be reviewed and updated regularly to ensure it remains current with the project's evolution and best practices.*
