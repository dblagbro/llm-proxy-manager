/**
 * SMTP Email Notifications Module
 * Sends email alerts for system failures and critical events
 */

const nodemailer = require('nodemailer');
const EventEmitter = require('events');

class NotificationManager extends EventEmitter {
  constructor(logger, config) {
    super();
    this.logger = logger;
    this.config = config;
    this.transporter = null;
    this.enabled = process.env.SMTP_ENABLED === 'true';

    this.smtpConfig = {
      host: process.env.SMTP_HOST || 'localhost',
      port: parseInt(process.env.SMTP_PORT || '587'),
      secure: process.env.SMTP_SECURE === 'true', // true for 465, false for other ports
      auth: {
        user: process.env.SMTP_USER || '',
        pass: process.env.SMTP_PASS || ''
      }
    };

    this.emailConfig = {
      from: process.env.SMTP_FROM || 'llm-proxy@localhost',
      to: process.env.SMTP_TO || '',
      alertSubjectPrefix: process.env.SMTP_SUBJECT_PREFIX || '[LLM Proxy Alert]'
    };

    // Alert throttling to prevent email storms
    this.alertThrottle = new Map(); // alertKey -> lastSentTimestamp
    this.throttleWindow = parseInt(process.env.ALERT_THROTTLE_MINUTES || '15') * 60 * 1000;

    // Alert severity levels
    this.severityLevels = {
      INFO: 1,
      WARNING: 2,
      ERROR: 3,
      CRITICAL: 4
    };

    // Minimum severity to send emails
    this.minSeverity = this.getSeverityLevel(process.env.SMTP_MIN_SEVERITY || 'WARNING');

    if (this.enabled) {
      this.initialize();
    } else {
      this.logger.info('Email notifications: DISABLED');
    }
  }

  getSeverityLevel(name) {
    return this.severityLevels[name.toUpperCase()] || this.severityLevels.WARNING;
  }

  initialize() {
    if (!this.emailConfig.to) {
      this.logger.warn('SMTP notifications enabled but SMTP_TO not configured');
      this.enabled = false;
      return;
    }

    try {
      this.transporter = nodemailer.createTransport(this.smtpConfig);

      // Verify SMTP connection
      this.transporter.verify((error, success) => {
        if (error) {
          this.logger.error(`SMTP connection failed: ${error.message}`);
          this.enabled = false;
        } else {
          this.logger.info(`Email notifications: ENABLED (${this.emailConfig.to})`);
          this.sendEmail(
            'LLM Proxy Manager Started',
            'Email notification system is active and monitoring for failures.',
            'INFO'
          );
        }
      });
    } catch (error) {
      this.logger.error(`Failed to initialize SMTP: ${error.message}`);
      this.enabled = false;
    }
  }

  shouldSendAlert(alertKey, severity) {
    if (!this.enabled) return false;

    // Check severity threshold
    if (severity < this.minSeverity) {
      return false;
    }

    // Check throttling
    const lastSent = this.alertThrottle.get(alertKey);
    const now = Date.now();

    if (lastSent && (now - lastSent) < this.throttleWindow) {
      return false; // Too soon, throttle this alert
    }

    return true;
  }

  async sendEmail(subject, body, severity = 'INFO', alertKey = null) {
    if (!this.enabled) return;

    const severityLevel = this.getSeverityLevel(severity);

    // Use alertKey for throttling if provided
    const throttleKey = alertKey || subject;

    if (!this.shouldSendAlert(throttleKey, severityLevel)) {
      return;
    }

    const fullSubject = `${this.emailConfig.alertSubjectPrefix} ${subject}`;

    const htmlBody = this.formatEmailBody(subject, body, severity);

    try {
      await this.transporter.sendMail({
        from: this.emailConfig.from,
        to: this.emailConfig.to,
        subject: fullSubject,
        text: body,
        html: htmlBody
      });

      this.alertThrottle.set(throttleKey, Date.now());
      this.logger.info(`Email alert sent: ${subject}`);
      this.emit('email.sent', { subject, severity });

    } catch (error) {
      this.logger.error(`Failed to send email: ${error.message}`);
      this.emit('email.failed', { subject, error: error.message });
    }
  }

  formatEmailBody(subject, body, severity) {
    const severityColors = {
      INFO: '#3b82f6',
      WARNING: '#f59e0b',
      ERROR: '#ef4444',
      CRITICAL: '#dc2626'
    };

    const color = severityColors[severity.toUpperCase()] || severityColors.INFO;
    const timestamp = new Date().toISOString();

    return `
<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background: ${color}; color: white; padding: 20px; border-radius: 8px 8px 0 0; }
        .content { background: #f9f9f9; padding: 20px; border-radius: 0 0 8px 8px; }
        .severity { display: inline-block; background: ${color}; color: white; padding: 4px 12px; border-radius: 4px; font-size: 12px; font-weight: bold; }
        .footer { margin-top: 20px; padding-top: 20px; border-top: 1px solid #ddd; font-size: 12px; color: #666; }
        .timestamp { color: #666; font-size: 14px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2 style="margin: 0;">🤖 LLM Proxy Manager Alert</h2>
        </div>
        <div class="content">
            <p><span class="severity">${severity.toUpperCase()}</span></p>
            <h3>${subject}</h3>
            <div style="white-space: pre-wrap; background: white; padding: 15px; border-left: 4px solid ${color}; border-radius: 4px;">
${body}
            </div>
            <div class="footer">
                <p class="timestamp">Alert generated: ${timestamp}</p>
                <p>This is an automated alert from your LLM Proxy Manager instance.</p>
            </div>
        </div>
    </div>
</body>
</html>
    `;
  }

  // Predefined alert templates
  alertCircuitBreakerOpen(provider, reason) {
    const subject = `Circuit Breaker OPEN: ${provider.name}`;
    const body = `
Provider: ${provider.name} (${provider.type})
Status: Circuit Breaker OPEN
Reason: ${reason}

The circuit breaker has opened for this provider due to repeated failures.
The provider will be automatically retried after the configured timeout period.

Action Required:
- Check the provider's API key and configuration
- Verify the provider's service status
- Check recent error logs for details
- Consider temporarily disabling this provider if issues persist
    `.trim();

    this.sendEmail(subject, body, 'ERROR', `circuit-open-${provider.id}`);
  }

  alertBillingError(provider, error) {
    const subject = `Billing/Quota Error: ${provider.name}`;
    const body = `
Provider: ${provider.name} (${provider.type})
Error Type: Billing/Quota Issue
Error Message: ${error}

A billing or quota-related error was detected for this provider.
This typically indicates:
- Insufficient API credits
- Rate limit exceeded
- Subscription expired
- Payment method issue

Action Required:
- Check your account balance/credits with ${provider.type}
- Verify your subscription status
- Review rate limits and usage
- Update payment information if needed
- Consider adding backup providers
    `.trim();

    this.sendEmail(subject, body, 'CRITICAL', `billing-error-${provider.id}`);
  }

  alertExternalServiceDown(providerType, status, incidents) {
    const subject = `External Service Issues: ${providerType}`;
    const body = `
Service: ${providerType}
Status: ${status.toUpperCase()}

The external service status page reports issues:
${incidents.map(i => `- ${i}`).join('\n')}

This may impact all ${providerType} providers configured in your proxy.

Action:
- Monitor the service status page
- Consider failing over to alternative providers
- Check https://status.${providerType}.com for updates
    `.trim();

    this.sendEmail(subject, body, 'WARNING', `external-down-${providerType}`);
  }

  alertAllProvidersDown() {
    const subject = 'CRITICAL: All Providers Unavailable';
    const body = `
CRITICAL ALERT: No providers are currently available!

All configured providers are either:
- Disabled
- Have circuit breakers open
- Experiencing failures

This means your LLM Proxy is currently unable to serve requests.

Immediate Action Required:
- Check all provider configurations
- Verify API keys are valid
- Check external service status
- Review error logs
- Consider adding new providers as backup
    `.trim();

    this.sendEmail(subject, body, 'CRITICAL', 'all-providers-down');
  }

  alertClusterNodeDown(peer) {
    const subject = `Cluster Node Unhealthy: ${peer.name}`;
    const body = `
Cluster Node: ${peer.name} (${peer.id})
Status: UNHEALTHY
Last Heartbeat: ${peer.lastHeartbeat || 'Never'}

A peer node in your cluster is not responding to heartbeats.

Potential Issues:
- Network connectivity problems
- Node crashed or restarted
- Firewall blocking cluster traffic
- Node under heavy load

Action:
- Check node's health and logs
- Verify network connectivity between nodes
- Ensure cluster authentication is configured correctly
    `.trim();

    this.sendEmail(subject, body, 'WARNING', `cluster-down-${peer.id}`);
  }

  // Test email functionality
  async sendTestEmail() {
    if (!this.enabled) {
      throw new Error('Email notifications are disabled');
    }

    await this.sendEmail(
      'Test Email - System Check',
      `This is a test email from your LLM Proxy Manager.

If you receive this email, your SMTP configuration is working correctly.

Configuration:
- SMTP Host: ${this.smtpConfig.host}
- SMTP Port: ${this.smtpConfig.port}
- From: ${this.emailConfig.from}
- To: ${this.emailConfig.to}
- Min Severity: ${Object.keys(this.severityLevels).find(k => this.severityLevels[k] === this.minSeverity)}

Timestamp: ${new Date().toISOString()}`,
      'INFO',
      `test-${Date.now()}` // Unique key to bypass throttling
    );
  }
}

module.exports = NotificationManager;
