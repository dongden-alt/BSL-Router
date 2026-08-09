import json

def test_json_determinism():
    # Payload 1: standard order
    payload1 = {
        "model": "glm-4",
        "messages": [{"role": "system", "content": "You are a helpful assistant."}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Searches the web",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}
                }
            }
        ]
    }

    # Payload 2: keys are completely scrambled
    payload2 = {
        "tools": [
            {
                "function": {
                    "parameters": {"properties": {"query": {"type": "string"}}, "type": "object"},
                    "description": "Searches the web",
                    "name": "search"
                },
                "type": "function"
            }
        ],
        "messages": [{"content": "You are a helpful assistant.", "role": "system"}],
        "model": "glm-4"
    }

    # Standard json.dumps will NOT match
    standard1 = json.dumps(payload1).encode("utf-8")
    standard2 = json.dumps(payload2).encode("utf-8")
    assert standard1 != standard2, "Standard json.dumps should fail a byte match!"

    # Deterministic json.dumps MUST match exactly
    deterministic1 = json.dumps(payload1, sort_keys=True).encode("utf-8")
    deterministic2 = json.dumps(payload2, sort_keys=True).encode("utf-8")
    
    assert deterministic1 == deterministic2, "Deterministic JSON failed to match!"
    print("SUCCESS: Deterministic JSON sorting guarantees 100% byte-level cache prefix stability.")

if __name__ == "__main__":
    test_json_determinism()
