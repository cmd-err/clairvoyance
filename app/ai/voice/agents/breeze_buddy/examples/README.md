# Breeze Buddy Examples

This directory contains example templates and utilities for the Breeze Buddy voice agent system.

## Directory Structure

```
examples/
├── templates/              # Template JSON examples
│   ├── order-confirmation.json
│   ├── curl-hook-example.json
│   └── order-confirmation-with-curl.json
├── test_curl_hook.py      # Unit tests for curl_hook
└── verify_curl_hook.py    # Verification script
```

## Template Examples

### order-confirmation.json
The original, production-ready order confirmation template with:
- Greeting and availability check
- Order verification
- Address update capability
- Cancellation flow
- Hooks for database updates

**Use case**: Standard order confirmation calls for e-commerce

### curl-hook-example.json
Basic examples demonstrating curl_hook feature with:
- Simple POST requests
- GET requests for data fetching
- Static field configuration
- LLM-inferred field usage

**Use case**: Learning and testing curl_hook functionality

### order-confirmation-with-curl.json
Enhanced order confirmation template with curl_hook integration showing:
- Webhook notifications for each event
- Status updates to external systems
- Mixed static and dynamic data
- Real-world integration patterns

**Use case**: Order confirmation with external system integration

## Verification & Testing

### verify_curl_hook.py
Automated verification script that checks:
- Python syntax validity
- CurlHook implementation completeness
- JSON template validity
- Documentation existence

**Run**:
```bash
cd /home/runner/work/clairvoyance/clairvoyance
python app/ai/voice/agents/breeze_buddy/examples/verify_curl_hook.py
```

### test_curl_hook.py
Unit tests for curl_hook with mocked HTTP requests testing:
- Static field configuration
- LLM-inferred fields
- Different HTTP methods
- Error handling
- Hook registry

**Note**: Requires full environment with pipecat dependencies

## Quick Start

### 1. Using a Template

Load a template into the database:
```bash
POST /api/v1/breeze-buddy/template
Content-Type: application/json

{
  "merchant": "your-merchant-id",
  "template_name": "order-confirmation",
  "flow": { ... }
}
```

### 2. Creating a Lead

Create a lead using the template:
```bash
POST /api/v1/breeze-buddy/push/lead/v2
Content-Type: application/json

{
  "merchant": "your-merchant-id",
  "template": "order-confirmation",
  "payload": {
    "customer_name": "John Doe",
    "order_id": "12345",
    ...
  }
}
```

### 3. Using curl_hook

Add curl_hook to any function in your template:
```json
{
  "function_name": "confirm_order",
  "hooks": [
    {
      "name": "curl_hook",
      "expected_fields": {
        "url": {
          "source": "static",
          "value": "https://your-api.com/webhook"
        },
        "method": {
          "source": "static",
          "value": "POST"
        },
        "body": {
          "source": "static",
          "value": {
            "order_id": "{order_id}",
            "status": "confirmed"
          }
        }
      }
    }
  ]
}
```

## Documentation

### Comprehensive Guides
- **curl_hook Usage**: `docs/CURL_HOOK_USAGE.md`
- **Implementation Summary**: `docs/CURL_HOOK_IMPLEMENTATION_SUMMARY.md`
- **Architecture**: `docs/BREEZE_BUDDY_ARCHITECTURE.md`

### Quick Reference

**curl_hook Parameters**:
- `url` (required): Endpoint URL
- `method` (optional): GET, POST, PUT, PATCH, DELETE (default: POST)
- `headers` (optional): Request headers dict
- `body` (optional): Request body dict
- `timeout` (optional): Timeout in seconds (default: 10)

**Field Sources**:
- `static`: Fixed value in template
- `llm`: Value from LLM function arguments

**Template Variables**:
- Use `{variable_name}` in static values
- Defined in `expected_payload_schema`
- Substituted at template load time

## Common Patterns

### Pattern 1: Webhook Notification
```json
{
  "name": "curl_hook",
  "expected_fields": {
    "url": {"source": "static", "value": "{webhook_url}"},
    "method": {"source": "static", "value": "POST"},
    "body": {"source": "static", "value": {"event": "order_confirmed"}}
  }
}
```

### Pattern 2: LLM Data Capture
```json
{
  "name": "curl_hook",
  "expected_fields": {
    "url": {"source": "static", "value": "https://api.example.com/feedback"},
    "body": {"source": "static", "value": {"order_id": "{order_id}"}},
    "feedback_text": {"source": "llm"}
  }
}
```

### Pattern 3: External Data Fetch
```json
{
  "name": "curl_hook",
  "expected_fields": {
    "url": {"source": "static", "value": "https://api.example.com/customer/{customer_id}"},
    "method": {"source": "static", "value": "GET"},
    "headers": {"source": "static", "value": {"Authorization": "Bearer {api_token}"}}
  }
}
```

## Troubleshooting

### Hook Not Executing
1. Check hook name is exactly `"curl_hook"`
2. Verify `url` field is present in `expected_fields`
3. Check application logs for error messages
4. Run `verify_curl_hook.py` to validate implementation

### HTTP Request Fails
1. Test URL manually with curl or Postman
2. Verify authentication headers
3. Check timeout setting
4. Review external API logs
5. Check application logs for detailed error

### Data Not Sent
1. Verify field sources (static vs llm)
2. Check template variable substitution
3. Ensure LLM function has required properties
4. Review request body in logs

## Testing Tips

### Use Mock Endpoints
Test your configuration with:
- [RequestBin](https://requestbin.com/)
- [webhook.site](https://webhook.site/)
- [Mockoon](https://mockoon.com/)

### Verify Template Before Deploy
```bash
python app/ai/voice/agents/breeze_buddy/examples/verify_curl_hook.py
```

### Check Logs
Enable debug logging to see:
- Request configuration
- Response status and body
- Error messages

## Best Practices

1. **Security**
   - Use HTTPS URLs
   - Store API keys in environment variables
   - Don't hardcode sensitive data in templates

2. **Performance**
   - Set appropriate timeouts (5-30 seconds)
   - Use GET for read operations
   - Consider rate limits

3. **Reliability**
   - Design external APIs to handle missing webhooks
   - Log all requests for debugging
   - Monitor error rates

4. **Maintainability**
   - Document webhook formats
   - Use template variables for flexibility
   - Keep request bodies simple

## Support

For questions or issues:
1. Check the documentation in `docs/`
2. Review the examples in this directory
3. Run the verification script
4. Check application logs

## Contributing

When adding new examples:
1. Follow existing template structure
2. Add inline comments for clarity
3. Validate JSON syntax
4. Update this README
5. Test with verification script

---

**Last Updated**: December 18, 2025  
**Version**: 1.0.0
