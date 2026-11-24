import asyncio
import threading
from typing import Any, Dict, Optional, Type, Union
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from crewai.tools import BaseTool

from shared.harness.mcp import MCPClient


class AgentBudgetState(BaseModel):
    """Shared state injected into tools to track calls made by a specific agent."""
    tool_calls_made: int = 0
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def increment(self) -> int:
        with self._lock:
            self.tool_calls_made += 1
            return self.tool_calls_made


class MCPToolWrapper(BaseTool):
    """A CrewAI/Langchain compatible BaseTool that wraps an MCPClient endpoint."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    args_schema: Type[BaseModel]
    
    # Internal fields not part of the tool args
    mcp_client: MCPClient = Field(exclude=True)
    mcp_tool_name: str = Field(exclude=True)
    agent_state: AgentBudgetState = Field(exclude=True)

    def _run(self, **kwargs: Any) -> Any:
        """Synchronous execution fallback. CrewAI agents execute in a worker thread."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._arun(**kwargs))
            
        raise RuntimeError("Called sync _run from an async context. Use _arun instead.")

    async def _arun(self, **kwargs: Any) -> Any:
        """Asynchronous execution calling the underlying MCPClient."""
        # Enforce budget tracking
        self.agent_state.increment()

        try:
            # We pass kwargs as the arguments dict to the MCP tool
            result = await self.mcp_client.call_tool(self.mcp_tool_name, arguments=kwargs)
            
            # The official MCP SDK returns a CallToolResult which gets model_dump()ed.
            # We usually want to return the text content so the LLM can read it.
            if isinstance(result, dict) and "content" in result:
                # Extract text content from MCP format
                texts = []
                for item in result["content"]:
                    if item.get("type") == "text":
                        texts.append(item.get("text", ""))
                return "\n".join(texts)
            
            return str(result)
        except Exception as e:
            return f"Error executing tool {self.mcp_tool_name}: {str(e)}"


def _resolve_json_schema_type(prop_details: Dict[str, Any]) -> Any:
    """Resolve JSON Schema types (including FastMCP anyOf/oneOf optionals) to Python types."""
    type_map: Dict[str, Any] = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": Dict[str, Any],
    }
    if not isinstance(prop_details, dict):
        return Any

    if "type" in prop_details:
        raw_t = prop_details.get("type")
        if isinstance(raw_t, list):
            non_null = [type_map.get(t, Any) for t in raw_t if t != "null"]
            if len(non_null) == 1:
                return non_null[0]
            if len(non_null) > 1:
                return Union[tuple(non_null)]
        return type_map.get(raw_t, Any)

    for union_key in ("anyOf", "oneOf"):
        branches = prop_details.get(union_key)
        if isinstance(branches, list):
            resolved = [
                _resolve_json_schema_type(b)
                for b in branches
                if isinstance(b, dict) and b.get("type") != "null"
            ]
            resolved = [t for t in resolved if t is not Any]
            if len(resolved) == 1:
                return resolved[0]
            if len(resolved) > 1:
                return Union[tuple(resolved)]

    return Any


def _build_pydantic_model_from_schema(schema: Dict[str, Any], model_name: str) -> Type[BaseModel]:
    """Dynamically build a Pydantic BaseModel from an MCP tool JSON schema."""
    from pydantic import create_model, ConfigDict

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    fields = {}
    for prop_name, prop_details in properties.items():
        prop_type = _resolve_json_schema_type(prop_details)

        # Determine if field is required
        if prop_name in required:
            fields[prop_name] = (prop_type, Field(..., description=prop_details.get("description", "")))
        else:
            fields[prop_name] = (Optional[prop_type], Field(None, description=prop_details.get("description", "")))

    return create_model(model_name, __config__=ConfigDict(arbitrary_types_allowed=True), **fields)


async def build_mcp_tools(mcp_client: MCPClient, agent_state: AgentBudgetState) -> list[MCPToolWrapper]:
    """Fetch tools from the MCP server and wrap them for CrewAI."""
    tools_list = await mcp_client.list_tools()
    
    wrappers = []
    for tool_def in tools_list:
        tool_name = tool_def["name"]
        
        # Build Pydantic model for args_schema
        schema_model = _build_pydantic_model_from_schema(
            tool_def.get("inputSchema", {}),
            model_name=f"{tool_name.capitalize()}Schema"
        )
        
        wrapper = MCPToolWrapper(
            name=tool_name,
            description=tool_def.get("description", f"Executes the {tool_name} tool"),
            args_schema=schema_model,
            mcp_client=mcp_client,
            mcp_tool_name=tool_name,
            agent_state=agent_state
        )
        wrappers.append(wrapper)
        
    return wrappers

