/** MCP's stdio framing: one JSON-RPC message per line.
 *
 * Kept apart from the forwarding so the framing can be tested without a
 * Runtime and the forwarding without a pipe. Both halves have failure modes
 * the other cannot cause: a line that is not JSON is this file's problem, and
 * a Runtime that will not start is the bridge's.
 */

export interface JsonRpcMessage {
  jsonrpc: '2.0'
  id?: string | number | null
  method?: string
  params?: unknown
  result?: unknown
  error?: { code: number; message: string; data?: unknown }
}

/** JSON-RPC's own codes, for the two things that can go wrong out here. */
export const PARSE_ERROR = -32700
export const INTERNAL_ERROR = -32603

/**
 * Split a stream of bytes into whole lines, keeping the remainder.
 *
 * A chunk boundary is not a message boundary — a reader that treats every
 * `data` event as a message will one day cut a request in half — so the tail
 * is carried until its newline arrives.
 */
export class LineReader {
  private held = ''

  push(chunk: string): string[] {
    this.held += chunk
    const lines = this.held.split('\n')
    // The last piece has no newline yet: it is either an empty string after a
    // clean break, or the start of a message still arriving.
    this.held = lines.pop() ?? ''
    return lines.map(line => line.trim()).filter(line => line !== '')
  }

  /** Anything left when the stream ends; a truncated message is not a message. */
  rest(): string { return this.held.trim() }
}

/** One message, framed. The newline is the frame, so it is added here rather
 *  than remembered at each call site. */
export function frame(message: JsonRpcMessage): string {
  return `${JSON.stringify(message)}\n`
}

/**
 * Read one line into a message, or say why it could not be.
 *
 * A parse failure is answered rather than thrown: the peer is waiting, and a
 * server that dies on a malformed line takes the whole session with it.
 */
export function parse(line: string): JsonRpcMessage | { parseError: string } {
  try {
    const value: unknown = JSON.parse(line)
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
      return { parseError: 'a JSON-RPC message must be an object' }
    }
    return value as JsonRpcMessage
  } catch (error) {
    return { parseError: error instanceof Error ? error.message : String(error) }
  }
}

/** A failure sent back in the shape the caller is waiting for. */
export function errorReply(
  id: JsonRpcMessage['id'], code: number, message: string,
): JsonRpcMessage {
  return { jsonrpc: '2.0', id: id ?? null, error: { code, message } }
}

/** Whether a message expects an answer. Notifications carry no id and must not
 *  be replied to — answering one is a message the peer never asked for. */
export function expectsReply(message: JsonRpcMessage): boolean {
  return message.id !== undefined && message.id !== null
}
