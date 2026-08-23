import type { Context } from '@deepseek-ai/cordis';
import { OrbitGateway } from './gateway.js';
export declare class OrbitToolBridge {
    private readonly ctx;
    private readonly gateway;
    private readonly tools;
    private readonly registry;
    constructor(ctx: Context, gateway: OrbitGateway);
    register(): void;
    private definitions;
    private definition;
    private command;
    private call;
    private route;
}
