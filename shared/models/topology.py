from typing import Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict


class TopologyNode(BaseModel):
    id: str
    name: str
    namespace: str
    type: str = "Deployment"
    replicas: int = 1
    resource_limits: Dict[str, str] = Field(default_factory=dict)
    ports: List[int] = Field(default_factory=list)
    labels: Dict[str, str] = Field(default_factory=dict)
    dependencies: List[str] = Field(default_factory=list)
    stub: bool = False


class TopologyEdge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_node: str = Field(..., alias="from")
    to_node: str = Field(..., alias="to")
    protocol: str = "TCP"
    port: Optional[int] = None


class SubGraph(BaseModel):
    target_node: TopologyNode
    neighbors: List[TopologyNode] = Field(default_factory=list)
    edges: List[TopologyEdge] = Field(default_factory=list)
    stubs: List[TopologyNode] = Field(default_factory=list)
    status: Optional[str] = None
    warning: Optional[str] = None
