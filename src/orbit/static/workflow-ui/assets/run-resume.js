export function humanResponseValue(interrupt, decision = "approve") {
  if (decision !== "approve" && decision !== "reject") {
    throw new TypeError("human decision must be approve or reject");
  }
  const ports = interrupt?.value?.output_ports;
  if (!Array.isArray(ports) || ports.length !== 1 || !ports[0]?.id) return {};
  return {
    [ports[0].id]: { decision, value: null },
  };
}

export function resumeActions(run, command, label = command.label) {
  const interrupts = Array.isArray(run?.interrupts) ? run.interrupts : [];
  if (interrupts.length === 0) {
    return [{
      command,
      label,
      nodeId: null,
      payload: { value: {} },
    }];
  }
  return interrupts.map((interrupt) => {
    const nodeId = interrupt?.value?.node_id;
    const suffix = interrupts.length > 1 && nodeId ? ` · ${nodeId}` : "";
    return {
      command,
      label: `${label}${suffix}`,
      // Reported rather than phrased: which node is a fact, and the sentence
      // asking about it belongs to whichever surface does the asking.
      nodeId: nodeId ?? null,
      payload: {
        interrupt_id: interrupt.id,
        value: humanResponseValue(interrupt),
      },
    };
  });
}
