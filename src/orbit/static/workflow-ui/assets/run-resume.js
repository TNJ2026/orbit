/** The single port an approval is answered on, or null if this is not one. */
export function approvalPort(interrupt) {
  const ports = interrupt?.value?.output_ports;
  if (!Array.isArray(ports) || ports.length !== 1) return null;
  const id = ports[0]?.id;
  return typeof id === "string" && id ? id : null;
}

/**
 * One approval answer, in the shape the Runtime takes.
 *
 * `value` is always written, `null` when there is nothing to say: an approval
 * submission whose keys are not exactly `decision` and `value` is refused.
 * When there is something to say it is a rejection's reason, and a workflow
 * that sends the work back to its Agent carries it across the back edge as
 * the next attempt's brief.
 */
export function humanResponseValue(interrupt, decision = "approve", reason = null) {
  if (decision !== "approve" && decision !== "reject") {
    throw new TypeError("human decision must be approve or reject");
  }
  const port = approvalPort(interrupt);
  if (port === null) return {};
  const said = typeof reason === "string" ? reason.trim() : "";
  return {
    [port]: { decision, value: said === "" ? null : said },
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
      // An approval is answered by choosing, so the caller draws two buttons
      // rather than one field of JSON. `suffix` is carried out rather than
      // only baked into `label`, because those buttons name themselves and
      // still have to say which node they are about.
      suffix,
      interrupt,
      approval: approvalPort(interrupt) !== null,
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
