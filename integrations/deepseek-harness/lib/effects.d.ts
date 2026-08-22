export interface WorkspaceEffects {
    baseRevision?: string;
    finalRevision?: string;
    changedFiles: string[];
    createdFiles: string[];
    deletedFiles: string[];
    generatedArtifacts: string[];
    commandCategories: string[];
    observation: 'git-status' | 'unavailable';
}
interface Entry {
    code: string;
    digest?: string;
}
export interface WorkspaceSnapshot {
    revision?: string;
    entries: Map<string, Entry>;
}
export declare function snapshotWorkspace(cwd: string): Promise<WorkspaceSnapshot | null>;
export declare function effectManifest(before: WorkspaceSnapshot | null, after: WorkspaceSnapshot | null): WorkspaceEffects;
export {};
