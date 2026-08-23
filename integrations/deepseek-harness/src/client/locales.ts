/** Panel copy, registered with the Harness locale service. */

export const ORBIT_LOCALE_NAMESPACE = 'orbit'

export const en = {
  title: 'Orbit',
  expand: 'Show Orbit runs',
  collapse: 'Hide Orbit runs',
  openRuntime: 'Open in Orbit',
  dock: 'Dock to the side',
  float: 'Detach',
  empty: 'No runs in this Workspace yet.',
  loading: 'Asking Orbit…',
  disconnected: 'No Orbit Runtime is serving this Workspace.',
  liveCount: '{live} running of {total}',
  idleCount: '{total} runs',
  status: 'Status',
  refresh: 'Refresh',
  noOutput: 'No output from this step.',
  needsPerson: 'Waiting for a person to confirm what the external Agent did.',
  cancel: 'Cancel run',
  resume: 'Continue',
  answer: 'What should it use?',
  confirmSucceeded: 'It succeeded',
  confirmFailed: 'It failed',
  note: 'What you checked',
  working: 'Working…',
} as const

export const zh = {
  title: 'Orbit',
  expand: '显示 Orbit 运行',
  collapse: '收起 Orbit 运行',
  openRuntime: '在 Orbit 中打开',
  dock: '停靠到侧边',
  float: '浮动',
  empty: '这个 Workspace 还没有运行记录。',
  loading: '正在询问 Orbit…',
  disconnected: '没有 Orbit Runtime 在服务这个 Workspace。',
  liveCount: '{total} 个运行中有 {live} 个进行中',
  idleCount: '{total} 个运行',
  status: '状态',
  refresh: '刷新',
  noOutput: '这个步骤没有输出。',
  needsPerson: '等待人工确认外部 Agent 的执行结果。',
  cancel: '取消运行',
  resume: '继续',
  answer: '要用什么值继续？',
  confirmSucceeded: '确认执行成功',
  confirmFailed: '确认执行失败',
  note: '你核对了什么',
  working: '处理中…',
} as const

export type OrbitLocaleKey = keyof typeof en
