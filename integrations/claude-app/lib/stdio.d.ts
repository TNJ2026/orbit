/** MCP's stdio framing: one JSON-RPC message per line.
 *
 * Kept apart from the forwarding so the framing can be tested without a
 * Runtime and the forwarding without a pipe. Both halves have failure modes
 * the other cannot cause: a line that is not JSON is this file's problem, and
 * a Runtime that will not start is the bridge's.
 */
export interface JsonRpcMessage {
    jsonrpc: '2.0';
    id?: string | number | null;
    method?: string;
    params?: unknown;
    result?: unknown;
    error?: {
        code: number;
        message: string;
        data?: unknown;
    };
}
/** JSON-RPC's own codes, for the two things that can go wrong out here. */
export declare const PARSE_ERROR = -32700;
export declare const INTERNAL_ERROR = -32603;
/**
 * Split a stream of bytes into whole lines, keeping the remainder.
 *
 * A chunk boundary is not a message boundary — a reader that treats every
 * `data` event as a message will one day cut a request in half — so the tail
 * is carried until its newline arrives.
 */
export declare class LineReader {
    private held;
    push(chunk: string): string[];
    /** Anything left when the stream ends; a truncated message is not a message. */
    rest(): string;
}
/** One message, framed. The newline is the frame, so it is added here rather
 *  than remembered at each call site. */
export declare function frame(message: JsonRpcMessage): string;
/**
 * Read one line into a message, or say why it could not be.
 *
 * A parse failure is answered rather than thrown: the peer is waiting, and a
 * server that dies on a malformed line takes the whole session with it.
 */
export declare function parse(line: string): JsonRpcMessage | {
    parseError: string;
};
/** A failure sent back in the shape the caller is waiting for. */
export declare function errorReply(id: JsonRpcMessage['id'], code: number, message: string): JsonRpcMessage;
/** Whether a message expects an answer. Notifications carry no id and must not
 *  be replied to — answering one is a message the peer never asked for. */
export declare function expectsReply(message: JsonRpcMessage): boolean;
