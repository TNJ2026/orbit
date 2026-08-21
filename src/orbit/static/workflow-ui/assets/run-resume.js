function humanResponseTemplate(interrupt) {
  const ports = interrupt?.value?.output_ports;
  if (!Array.isArray(ports) || ports.length !== 1 || !ports[0]?.id) return {};
  return {
    [ports[0].id]: { decision: "approve", value: null },
  };
}

export function resumeActions(run, command, label = command.label) {
  const interrupts = Array.isArray(run?.interrupts) ? run.interrupts : [];
  if (interrupts.length === 0) {
    return [{
      command,
      label,
      prompt: "Resume value (JSON)",
      payload: { value: {} },
    }];
  }
  return interrupts.map((interrupt) => {
    const nodeId = interrupt?.value?.node_id;
    const suffix = interrupts.length > 1 && nodeId ? ` · ${nodeId}` : "";
    return {
      command,
      label: `${label}${suffix}`,
      prompt: nodeId ? `Resume ${nodeId} value (JSON)` : "Resume value (JSON)",
      payload: {
        interrupt_id: interrupt.id,
        value: humanResponseTemplate(interrupt),
      },
    };
  });
}
