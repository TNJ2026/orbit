import type { DelegationDto, WorkspaceRef } from './types.js';
/** Host-observable policy checks that must pass before a Provider is started. */
export declare function delegationRefusal(workspace: WorkspaceRef, delegation: DelegationDto, registeredProviders: readonly string[]): string | undefined;
