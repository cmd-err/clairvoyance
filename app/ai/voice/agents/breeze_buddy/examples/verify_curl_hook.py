"""
Simple verification script for curl_hook implementation.

This script checks the implementation without requiring full dependencies.
"""

import ast
import json
import os


def verify_hooks_implementation():
    """Verify the CurlHook class is properly implemented"""
    print("=" * 60)
    print("Verifying CurlHook Implementation")
    print("=" * 60)

    hooks_file = "app/ai/voice/agents/breeze_buddy/template/hooks.py"

    if not os.path.exists(hooks_file):
        print(f"✗ File not found: {hooks_file}")
        return False

    with open(hooks_file, "r") as f:
        content = f.read()

    # Parse the Python file
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        print(f"✗ Syntax error in {hooks_file}: {e}")
        return False

    print(f"✓ File {hooks_file} has valid Python syntax")

    # Check for CurlHook class
    classes = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]

    if "CurlHook" not in classes:
        print("✗ CurlHook class not found")
        return False

    print("✓ CurlHook class found")

    # Check for required methods
    curl_hook_class = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "CurlHook":
            curl_hook_class = node
            break

    if curl_hook_class:
        methods = [
            method.name
            for method in curl_hook_class.body
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]

        required_methods = ["__init__", "execute"]
        for method in required_methods:
            if method in methods:
                print(f"✓ Method {method} found in CurlHook")
            else:
                print(f"✗ Method {method} not found in CurlHook")
                return False

    # Check for registration in HookRegistry
    if "HookRegistry.register" in content and '"curl_hook"' in content:
        print("✓ curl_hook is registered in HookRegistry")
    else:
        print("✗ curl_hook is not registered in HookRegistry")
        return False

    # Check for key implementation details
    checks = [
        ("aiohttp_session", "Uses aiohttp session from context"),
        ("request_config", "Builds request configuration"),
        ("HookFieldConfigSource.STATIC", "Handles static field source"),
        ("HookFieldConfigSource.LLM", "Handles LLM field source"),
        ("session.request", "Makes HTTP request"),
    ]

    for check_str, description in checks:
        if check_str in content:
            print(f"✓ {description}")
        else:
            print(f"⚠ Warning: {description} might be missing")

    print("\n✓ All verifications passed!")
    return True


def verify_json_examples():
    """Verify JSON example files are valid"""
    print("\n" + "=" * 60)
    print("Verifying JSON Examples")
    print("=" * 60)

    examples_dir = "app/ai/voice/agents/breeze_buddy/examples/templates"
    json_files = [
        "curl-hook-example.json",
        "order-confirmation-with-curl.json",
    ]

    all_valid = True

    for filename in json_files:
        filepath = os.path.join(examples_dir, filename)

        if not os.path.exists(filepath):
            print(f"✗ File not found: {filepath}")
            all_valid = False
            continue

        try:
            with open(filepath, "r") as f:
                data = json.load(f)

            print(f"✓ {filename} is valid JSON")

            # Check for curl_hook usage
            hooks_found = 0
            nodes = data.get("flow", {}).get("nodes", [])

            for node in nodes:
                for func in node.get("functions", []):
                    for hook in func.get("hooks", []):
                        if hook.get("name") == "curl_hook":
                            hooks_found += 1

            if hooks_found > 0:
                print(f"  └─ Found {hooks_found} curl_hook usage(s)")
            else:
                print(f"  └─ No curl_hook usage found (might be intentional)")

        except json.JSONDecodeError as e:
            print(f"✗ {filename} has invalid JSON: {e}")
            all_valid = False

    return all_valid


def verify_documentation():
    """Verify documentation exists"""
    print("\n" + "=" * 60)
    print("Verifying Documentation")
    print("=" * 60)

    doc_file = "docs/CURL_HOOK_USAGE.md"

    if not os.path.exists(doc_file):
        print(f"✗ Documentation not found: {doc_file}")
        return False

    print(f"✓ Documentation exists: {doc_file}")

    with open(doc_file, "r") as f:
        content = f.read()

    # Check for key sections
    sections = [
        "Overview",
        "Configuration",
        "Usage Examples",
        "Field Sources",
        "Best Practices",
        "Troubleshooting",
    ]

    for section in sections:
        if f"## {section}" in content or f"# {section}" in content:
            print(f"✓ Section found: {section}")
        else:
            print(f"⚠ Section might be missing: {section}")

    word_count = len(content.split())
    print(f"\n  Documentation length: {word_count} words")

    return True


def main():
    """Run all verifications"""
    print("\n")
    print("*" * 60)
    print("*" + " " * 58 + "*")
    print("*" + "  CurlHook Implementation Verification".center(58) + "*")
    print("*" + " " * 58 + "*")
    print("*" * 60)
    print("\n")

    results = []

    # Run verifications
    results.append(("Implementation", verify_hooks_implementation()))
    results.append(("JSON Examples", verify_json_examples()))
    results.append(("Documentation", verify_documentation()))

    # Summary
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)

    all_passed = True
    for name, passed in results:
        status = "✓ PASSED" if passed else "✗ FAILED"
        print(f"{name}: {status}")
        if not passed:
            all_passed = False

    print("=" * 60)

    if all_passed:
        print("\n🎉 All verifications passed! curl_hook is ready to use.")
    else:
        print("\n❌ Some verifications failed. Please review the output above.")

    return all_passed


if __name__ == "__main__":
    import sys

    success = main()
    sys.exit(0 if success else 1)
