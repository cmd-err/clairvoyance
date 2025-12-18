# Curl Hook - External API Integration Guide

## Overview

The `curl_hook` is a powerful feature in Breeze Buddy's template system that allows you to call external HTTP endpoints during conversation flows. This enables integration with third-party APIs, custom backend services, and external data sources without modifying the core application code.

## Table of Contents

1. [Basic Concepts](#basic-concepts)
2. [Configuration](#configuration)
3. [Usage Examples](#usage-examples)
4. [Field Sources](#field-sources)
5. [Best Practices](#best-practices)
6. [Troubleshooting](#troubleshooting)

---

## Basic Concepts

### What is curl_hook?

The `curl_hook` is a type of hook that executes HTTP requests asynchronously after a function is triggered by the LLM. Like other hooks, it runs in the background without blocking the conversation flow.

### When to Use curl_hook?

Use `curl_hook` when you need to:
- Notify external systems about conversation events
- Fetch custom data from external APIs during specific nodes
- Update customer records in third-party CRMs
- Trigger webhooks or background jobs
- Integrate with merchant-specific backend services

### How It Works

1. User triggers a function (e.g., "confirm_order")
2. The conversation immediately transitions to the next node
3. The `curl_hook` executes asynchronously in the background
4. External API receives the HTTP request
5. Response is logged (but doesn't block conversation)

---

## Configuration

### Hook Structure

The `curl_hook` is configured within the `hooks` array of a function in your template JSON:

```json
{
  "function_name": "your_function_name",
  "hooks": [
    {
      "name": "curl_hook",
      "expected_fields": {
        "url": {
          "source": "static",
          "value": "https://api.example.com/endpoint"
        },
        "method": {
          "source": "static",
          "value": "POST"
        },
        "headers": {
          "source": "static",
          "value": {
            "Authorization": "Bearer YOUR_TOKEN",
            "Content-Type": "application/json"
          }
        },
        "body": {
          "source": "static",
          "value": {
            "customer_id": "12345",
            "action": "confirmed"
          }
        },
        "timeout": {
          "source": "static",
          "value": 10
        }
      }
    }
  ]
}
```

### Required Fields

| Field | Required | Description | Default |
|-------|----------|-------------|---------|
| `url` | **Yes** | The endpoint URL to call | N/A |
| `method` | No | HTTP method (GET, POST, PUT, DELETE, PATCH) | POST |
| `headers` | No | Request headers as key-value pairs | {} |
| `body` | No | Request body (used with POST/PUT/PATCH) | {} |
| `timeout` | No | Request timeout in seconds | 10 |

### HTTP Methods Supported

- **GET**: Retrieve data from external API
- **POST**: Send data to external API
- **PUT**: Update resources
- **PATCH**: Partial update of resources
- **DELETE**: Delete resources

---

## Usage Examples

### Example 1: Simple POST Request

Send a confirmation to an external webhook:

```json
{
  "function_name": "confirm_order",
  "hooks": [
    {
      "name": "curl_hook",
      "expected_fields": {
        "url": {
          "source": "static",
          "value": "https://merchant-api.com/orders/confirm"
        },
        "method": {
          "source": "static",
          "value": "POST"
        },
        "headers": {
          "source": "static",
          "value": {
            "Authorization": "Bearer sk_test_123456",
            "Content-Type": "application/json"
          }
        },
        "body": {
          "source": "static",
          "value": {
            "order_id": "{order_id}",
            "status": "confirmed",
            "timestamp": "{{auto}}"
          }
        }
      }
    }
  ]
}
```

**Note on Template Variables**: Values in curly braces like `{order_id}` are template variables that are substituted during template loading (by `FlowConfigLoader.load_template()`). These variables must be defined in the template's `expected_payload_schema` and provided in the lead payload when the call is initiated. The substitution happens once at the start of the call, before any nodes are executed.

### Example 2: GET Request to Fetch Data

Retrieve customer information from an external API:

```json
{
  "function_name": "check_account_status",
  "hooks": [
    {
      "name": "curl_hook",
      "expected_fields": {
        "url": {
          "source": "static",
          "value": "https://crm.example.com/api/customers/{customer_id}"
        },
        "method": {
          "source": "static",
          "value": "GET"
        },
        "headers": {
          "source": "static",
          "value": {
            "Authorization": "Bearer YOUR_API_KEY",
            "Accept": "application/json"
          }
        }
      }
    }
  ]
}
```

### Example 3: Dynamic Data from LLM

Send cancellation reason inferred by LLM to external API:

```json
{
  "function_name": "cancel_order",
  "properties": {
    "cancellation_reason": {
      "type": "string",
      "description": "Reason for cancellation"
    }
  },
  "required": ["cancellation_reason"],
  "hooks": [
    {
      "name": "curl_hook",
      "expected_fields": {
        "url": {
          "source": "static",
          "value": "https://api.example.com/orders/cancel"
        },
        "method": {
          "source": "static",
          "value": "POST"
        },
        "body": {
          "source": "llm",
          "value": null
        }
      }
    }
  ]
}
```

In this example, the entire `body` is sourced from the LLM's function arguments.

### Example 4: Mixed Static and LLM Sources

Combine static configuration with LLM-inferred data:

```json
{
  "function_name": "update_address",
  "properties": {
    "new_address": {
      "type": "string",
      "description": "The updated delivery address"
    }
  },
  "required": ["new_address"],
  "hooks": [
    {
      "name": "curl_hook",
      "expected_fields": {
        "url": {
          "source": "static",
          "value": "https://api.example.com/update-address"
        },
        "method": {
          "source": "static",
          "value": "PUT"
        },
        "headers": {
          "source": "static",
          "value": {
            "Authorization": "Bearer API_KEY"
          }
        },
        "body": {
          "source": "static",
          "value": {
            "order_id": "{order_id}",
            "action": "address_update"
          }
        },
        "new_address": {
          "source": "llm"
        }
      }
    }
  ]
}
```

Here, `body` comes from static config, but `new_address` is added from LLM args.

### Example 5: Custom Timeout

For slow external APIs, increase timeout:

```json
{
  "name": "curl_hook",
  "expected_fields": {
    "url": {
      "source": "static",
      "value": "https://slow-api.example.com/process"
    },
    "timeout": {
      "source": "static",
      "value": 30
    }
  }
}
```

---

## Field Sources

### Static Source

Use `"source": "static"` when the value is known at template design time and doesn't change per call.

**Example:**
```json
{
  "url": {
    "source": "static",
    "value": "https://api.merchant.com/webhook"
  }
}
```

**When to use:**
- API endpoints
- Authentication tokens
- Fixed header values
- Static request body structure

### LLM Source

Use `"source": "llm"` when the value should come from the LLM's function arguments.

**Example:**
```json
{
  "cancellation_reason": {
    "source": "llm"
  }
}
```

The LLM extracts `cancellation_reason` from the conversation and passes it as a function argument, which is then included in the HTTP request.

**When to use:**
- User-provided data (addresses, phone numbers, names)
- Dynamic values inferred from conversation
- Variable request parameters
- Context-dependent information

### Template Variables

You can use template variables (from `expected_payload_schema`) in static values:

```json
{
  "body": {
    "source": "static",
    "value": {
      "customer_id": "{customer_id}",
      "shop_name": "{shop_name}"
    }
  }
}
```

These variables are substituted during template loading.

---

## Best Practices

### 1. Use Appropriate HTTP Methods

- **GET**: For read-only operations (fetching data)
- **POST**: For creating new resources or triggering actions
- **PUT/PATCH**: For updating existing resources
- **DELETE**: For removing resources

### 2. Handle Authentication Securely

**❌ Bad:**
```json
{
  "headers": {
    "source": "static",
    "value": {
      "Authorization": "Bearer hardcoded_token_in_template"
    }
  }
}
```

**✅ Good:**
Store tokens in environment variables or secure configuration, then reference them in the template system.

### 3. Set Reasonable Timeouts

- Default: 10 seconds
- Fast APIs: 5 seconds
- Slow/batch APIs: 30 seconds
- Never exceed 60 seconds (blocks the system)

### 4. Log Response Data

The hook automatically logs:
- Request method and URL
- Response status code
- First 500 characters of response body
- Errors and exceptions

Check logs to debug failed API calls.

### 5. Error Handling

The `curl_hook` is designed to fail gracefully:
- Errors are logged but don't crash the conversation
- Failed requests don't block node transitions
- HTTP 4xx/5xx responses are logged as warnings

### 6. Avoid Blocking Operations

Remember: Hooks are **asynchronous** and **fire-and-forget**. Don't use `curl_hook` if:
- You need the response data immediately
- The conversation flow depends on the API response
- You need to verify success before proceeding

For synchronous operations, consider using a custom handler instead.

### 7. Test with Mock Endpoints

Before deploying to production, test your curl_hook configuration with:
- [RequestBin](https://requestbin.com/)
- [webhook.site](https://webhook.site/)
- [Mockoon](https://mockoon.com/)

### 8. Rate Limiting

If calling external APIs frequently:
- Check the API's rate limits
- Implement exponential backoff in your backend
- Consider batching requests if possible

---

## Troubleshooting

### Hook Not Executing

**Symptoms:** No HTTP requests in logs, no API calls received.

**Possible Causes:**
1. Hook name typo: Must be exactly `"curl_hook"`
2. Missing `url` field in `expected_fields`
3. Hook not registered in `HookRegistry`

**Solution:**
```python
# Verify hook is registered
from app.ai.voice.agents.breeze_buddy.template.hooks import HookRegistry
print(HookRegistry.get_all())  # Should include 'curl_hook'
```

### HTTP Request Failing

**Symptoms:** Logs show "Error executing HTTP request"

**Possible Causes:**
1. Invalid URL
2. Network connectivity issues
3. Timeout too short
4. SSL certificate validation failed
5. Authentication failure

**Solution:**
- Check logs for specific error message
- Test URL manually with curl/Postman
- Increase timeout if needed
- Verify authentication headers

### Response is 4xx/5xx

**Symptoms:** Logs show warning about error status code

**Possible Causes:**
1. Invalid authentication token
2. Malformed request body
3. API endpoint doesn't exist
4. Rate limit exceeded

**Solution:**
- Check API documentation
- Verify request format matches API expectations
- Test with same payload using curl/Postman

### Request Body Not Sent

**Symptoms:** API receives empty body

**Possible Causes:**
1. `body` field missing from `expected_fields`
2. HTTP method is GET (doesn't support body)
3. LLM didn't provide required arguments

**Solution:**
- Ensure `body` is in `expected_fields`
- Use POST/PUT/PATCH for requests with body
- Check LLM function arguments in logs

### Template Variables Not Substituted

**Symptoms:** URL/body contains literal `{customer_id}` instead of value

**Possible Causes:**
1. Variable not in `expected_payload_schema`
2. Variable not provided in lead payload
3. Template not rendered correctly

**Solution:**
```python
# Check template rendering
print(self.template_vars)  # Should contain all expected variables
```

---

## Advanced Usage

### Dynamic URL with Variables

```json
{
  "url": {
    "source": "static",
    "value": "https://api.example.com/customers/{customer_id}/orders/{order_id}"
  }
}
```

Variables `{customer_id}` and `{order_id}` will be substituted from payload.

### Custom Headers per Merchant

Use template variables in headers:

```json
{
  "headers": {
    "source": "static",
    "value": {
      "Authorization": "Bearer {merchant_api_key}",
      "X-Merchant-ID": "{merchant_id}"
    }
  }
}
```

### Query Parameters in URL

Include query parameters directly in URL:

```json
{
  "url": {
    "source": "static",
    "value": "https://api.example.com/data?customer_id={customer_id}&type=order"
  }
}
```

---

## Complete Example

Here's a complete node configuration using `curl_hook`:

```json
{
  "node_name": "confirm_delivery_node",
  "task_messages": [
    {
      "role": "system",
      "content": "Ask the customer to confirm delivery address: {address}"
    }
  ],
  "functions": [
    {
      "function_name": "confirm_delivery",
      "description": "Customer confirms the delivery address is correct",
      "properties": {
        "delivery_notes": {
          "type": "string",
          "description": "Any special delivery instructions from customer"
        }
      },
      "required": [],
      "transition_to": "thank_you_node",
      "hooks": [
        {
          "name": "update_outcome_in_database",
          "expected_fields": {
            "outcome": {
              "source": "static",
              "value": "confirmed"
            }
          }
        },
        {
          "name": "curl_hook",
          "expected_fields": {
            "url": {
              "source": "static",
              "value": "https://merchant.example.com/api/delivery/confirm"
            },
            "method": {
              "source": "static",
              "value": "POST"
            },
            "headers": {
              "source": "static",
              "value": {
                "Authorization": "Bearer {merchant_api_token}",
                "Content-Type": "application/json"
              }
            },
            "body": {
              "source": "static",
              "value": {
                "order_id": "{order_id}",
                "customer_id": "{customer_id}",
                "status": "confirmed",
                "address": "{address}"
              }
            },
            "delivery_notes": {
              "source": "llm"
            },
            "timeout": {
              "source": "static",
              "value": 15
            }
          }
        }
      ]
    }
  ]
}
```

In this example:
- The hook will POST to the merchant's API
- Static fields (order_id, customer_id, status, address) come from template variables
- Dynamic field (delivery_notes) comes from LLM
- Custom timeout of 15 seconds
- Authentication token from template variable

---

## Security Considerations

### 1. Sensitive Data

**Never** hardcode sensitive data in templates:
- API keys
- Authentication tokens
- Passwords
- Private keys

Instead, use environment variables or secure configuration management.

### 2. HTTPS Only

Always use HTTPS URLs for sensitive data:
```json
✅ "https://api.example.com/secure"
❌ "http://api.example.com/insecure"
```

### 3. Input Validation

The external API should validate all incoming data. Don't trust LLM-inferred values blindly.

### 4. Rate Limiting

Implement rate limiting on your external endpoints to prevent abuse.

---

## Monitoring and Logging

### What Gets Logged

The `curl_hook` logs:

1. **Info Level:**
   - Hook execution start
   - HTTP request method and URL
   - Response status code
   - Hook completion

2. **Debug Level:**
   - Field extraction (static vs LLM)
   - Request configuration
   - First 500 chars of response body

3. **Warning Level:**
   - HTTP 4xx/5xx responses
   - Missing LLM arguments
   - Missing expected fields

4. **Error Level:**
   - Missing URL
   - No aiohttp session
   - Network errors
   - Exceptions

### Log Format Example

```
INFO: Executing HTTP POST request to https://api.example.com/webhook for function 'confirm_order'
DEBUG: Field 'url': using static value 'https://api.example.com/webhook' for function 'confirm_order'
DEBUG: Field 'cancellation_reason': using LLM-inferred value 'Out of stock' for function 'confirm_order'
INFO: HTTP POST request to https://api.example.com/webhook completed with status 200 for function 'confirm_order'
```

---

## Summary

The `curl_hook` enables powerful external integrations without code changes:

✅ **Benefits:**
- No code deployment needed
- Configurable per merchant
- Asynchronous (non-blocking)
- Flexible field sources
- Comprehensive logging

⚠️ **Limitations:**
- Fire-and-forget (can't use response in conversation)
- Not suitable for synchronous operations
- Requires external endpoint to be available

📚 **Next Steps:**
1. Review the example template: `curl-hook-example.json`
2. Test with a mock endpoint
3. Integrate with your backend API
4. Monitor logs for troubleshooting

For questions or issues, refer to the [BREEZE_BUDDY_ARCHITECTURE.md](./BREEZE_BUDDY_ARCHITECTURE.md) documentation.
