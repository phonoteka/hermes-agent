"""S23: plugin launch keeps workflow input separate from execution-root authority."""
from pathlib import Path
import json
from plugins.canon.transport import build_run_request


def test_launch_request_separates_semantic_input_and_execution_root(tmp_path: Path):
    schema = tmp_path / "task-input.schema.json"
    schema.write_text(json.dumps({"type": "string", "pattern": "\\S"}))
    routing = tmp_path / "model-routing.json"
    routing.write_text(json.dumps({
        "routingId": "gateway-routing",
        "failClosed": True,
        "profiles": {"default": {"hermesProfileId": "gateway-hermes"}},
    }))
    workflow = tmp_path / "workflow.json"
    workflow.write_text(json.dumps({
        "workflowId": "gateway-workflow",
        "workflowInputPorts": [{
            "portId": "task",
            "schemaRef": schema.name,
            "logicalArtifactRef": "artifact:workflow-input/task",
            "deliveryMode": "inline-candidate",
        }],
        "policyRefs": {"modelRoutingRef": routing.name},
    }))
    request = build_run_request(
        workflow_path=workflow, workflow_input={"task": "hello"}, execution_root=tmp_path,
    )
    assert request["workflowInput"] == {"task": "hello"}
    assert request["runConfiguration"]["executionRoot"] == str(tmp_path)
    assert "executionRoot" not in request["workflowInput"]
    assert "runId" not in request and "threadId" not in request
