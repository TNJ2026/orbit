import { memo, useMemo } from 'react'
import {
  Background, Controls, Handle, MarkerType, Position, ReactFlow,
  type Edge, type Node, type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import styles from './OrbitPanel.module.css'

export interface WorkflowGraphNode {
  node_id: string
  kind: string
  label?: string
  handler_name?: string | null
  handler_version?: string | null
}

export interface WorkflowGraphEdge {
  edge_id: string
  from: string
  to: string
  route?: string
  back_edge?: boolean
}

export interface WorkflowGraph {
  nodes?: readonly WorkflowGraphNode[]
  edges?: readonly WorkflowGraphEdge[]
  layout?: { positions?: readonly { node_id: string; depth: number; lane: number }[] }
}

interface GraphNodeData extends Record<string, unknown> {
  kind: string
  label: string
  handler?: string
}

const DEPTH_WIDTH = 320
const LANE_HEIGHT = 140

const WorkflowGraphNodeCard = memo(function WorkflowGraphNodeCard({ data }: NodeProps<Node<GraphNodeData>>) {
  return (
    <div className={`${styles.graphNode} ${styles[`graphNode_${data.kind}`] ?? ''}`}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <span className={styles.graphNodeKind}>{data.kind}</span>
      <span className={styles.graphNodeTitle}>{data.label}</span>
      {data.handler ? <span className={styles.graphNodeHandler}>{data.handler}</span> : null}
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  )
})

const nodeTypes = { workflow: WorkflowGraphNodeCard }

export function OrbitWorkflowGraph({ graph }: { graph: WorkflowGraph }) {
  const model = useMemo(() => {
    const positions = new Map(
      (graph.layout?.positions ?? []).map(item => [item.node_id, item] as const),
    )
    const nodes: Node<GraphNodeData>[] = (graph.nodes ?? []).map(node => {
      const spot = positions.get(node.node_id) ?? { depth: 0, lane: 0 }
      return {
        id: node.node_id,
        type: 'workflow',
        position: { x: spot.depth * DEPTH_WIDTH, y: spot.lane * LANE_HEIGHT },
        draggable: false,
        selectable: false,
        connectable: false,
        data: {
          kind: node.kind,
          label: node.label ?? node.node_id,
          handler: node.handler_name
            ? `${node.handler_name}${node.handler_version ? ` ${node.handler_version}` : ''}`
            : undefined,
        },
      }
    })
    const edges: Edge[] = (graph.edges ?? []).map(edge => ({
      id: edge.edge_id,
      source: edge.from,
      target: edge.to,
      label: edge.route ?? 'success',
      animated: Boolean(edge.back_edge),
      markerEnd: { type: MarkerType.ArrowClosed },
      selectable: false,
    }))
    return { nodes, edges }
  }, [graph])

  return (
    <div className={styles.workflowGraph} aria-label="Workflow graph">
      <ReactFlow
        nodes={model.nodes}
        edges={model.edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2, minZoom: 0.5, maxZoom: 1 }}
        minZoom={0.5}
        maxZoom={2}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        deleteKeyCode={null}
        panOnDrag
        zoomOnPinch
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}
