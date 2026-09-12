import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
/** Controlled inference only; Electron, gateway, AIAgent, tools and persistence are real. */
import http from 'node:http'
import path from 'node:path'

import {
  buildAppEnv, createSandbox, launchDesktop, PACKAGED_BINARY_PATH,
  waitForAppReady, writeEnvFile, writeMockProviderConfig,
} from './fixtures'
import { _electron, allowErrorBanners, expect, installErrorBannerGuard, type Page, test } from './test'

const SILENT = 'E2E quiet receipt'
const NORMAL = 'E2E useful answer'
const FAILURE = 'E2E visible failure'
const TASK = 'Verify the useful outcome'
const VISIBLE = 'NO_REPLY is a literal marker; this useful explanation must remain visible.'

test('intentional silence survives streaming, completion and cold reopen', async ({ browserName }, testInfo) => {
  testInfo.annotations.push({ type: 'browser', description: browserName })
  test.setTimeout(240_000)
  const sandbox = createSandbox('intentional-silence')
  const requests: Array<{ messages: Array<{ role: string; content: string }>; stream?: boolean }> = []
  const pending: Array<() => void> = []
  let prefixSent = false
  let toolSent = false

  const server = http.createServer((req, res) => {
    if (req.method === 'GET') {
      res.setHeader('Content-Type', 'application/json')
      res.end(JSON.stringify({ object: 'list', data: [{ id: 'mock-model', object: 'model', owned_by: 'fixture' }] }))

      return
    }

    if (!req.url?.endsWith('/chat/completions')) {
      res.writeHead(404, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ error: 'Only chat completions supported' }))

      return
    }

    let body = ''
    req.on('data', chunk => { body += chunk.toString() })
    req.on('end', () => {
      const request = JSON.parse(body)
      requests.push(request)
      const lastUser = [...request.messages].reverse().find(message => message.role === 'user')?.content ?? ''
      console.log('Fixture request', { stream: request.stream, lastUser: String(lastUser).slice(0, 180), roles: request.messages.map((message: { role: string }) => message.role) })
      console.log('Fixture tool result', request.messages.filter((message: { role: string }) => message.role === 'tool'))

      if (!request.stream) {
        res.writeHead(200, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ id: 'fixture-auxiliary', object: 'chat.completion', created: 0, model: 'mock-model', choices: [{ index: 0, message: { role: 'assistant', content: 'Ready' }, finish_reason: 'stop' }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } }))

        return
      }

      if (lastUser.includes(FAILURE)) {
        res.writeHead(400, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ error: { message: 'E2E deliberate provider failure', type: 'invalid_request_error' } }))

        return
      }

      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' })

      const frame = (delta: Record<string, unknown>, finish: string | null = null) => {
        res.write(`data: ${JSON.stringify({ id: 'fixture-completion', object: 'chat.completion.chunk', created: 0, model: 'mock-model', choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`)
      }

      const finish = (reason = 'stop') => {
        frame({}, reason)
        res.end('data: [DONE]\n\n')
      }

      if (lastUser.includes(SILENT) && !toolSent) {
        toolSent = true
        frame({ role: 'assistant', tool_calls: [{ index: 0, id: 'call_quiet_task', type: 'function', function: { name: 'tool_call', arguments: JSON.stringify({ name: 'todo_list', arguments: { todos: [{ id: 'verify', content: TASK, status: 'in_progress' }] } }) } }] })
        finish('tool_calls')
      } else if (lastUser.includes(SILENT)) {
        frame({ role: 'assistant', content: 'NO_' })
        prefixSent = true
        pending.push(() => {
          frame({ content: 'RE' })
          frame({ content: 'PLY' })
          finish()
        })
      } else {
        frame({ role: 'assistant', content: 'NO_' })
        frame({ content: VISIBLE.slice(3) })
        finish()
      }
    })
  })

  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address() as { port: number }
  writeMockProviderConfig(sandbox.hermesHome, `http://127.0.0.1:${address.port}/v1`)
  fs.appendFileSync(path.join(sandbox.hermesHome, 'config.yaml'), '\nauxiliary:\n  title_generation:\n    enabled: false\nagent:\n  verify_on_stop: false\n')
  writeEnvFile(sandbox.hermesHome)
  const env = buildAppEnv(sandbox)

  // This is an isolated interactive client, never the invoking Kanban worker.
  // Retaining the worker identity injects board guards into the mock conversation
  // and misroutes its gateway events to the real worker's session.
  const sandboxKeys = new Set(['HERMES_HOME', 'HERMES_DESKTOP_USER_DATA_DIR',
    'HERMES_DESKTOP_IGNORE_EXISTING', 'HERMES_DESKTOP_HERMES_ROOT',
    'HERMES_DESKTOP_APP_NAME', 'HERMES_DESKTOP_SKIP_QUIT_CONFIRM'])

  for (const key of Object.keys(env)) {
    if (key.startsWith('HERMES_') && !sandboxKeys.has(key)) { delete env[key] }
  }

  // Use the exact packaged candidate when available; otherwise exercise the built source client.
  const packaged = fs.existsSync(PACKAGED_BINARY_PATH)

  const launch = async () => {
    if (!packaged) { return launchDesktop(env) }
    const app = await _electron.launch({ executablePath: PACKAGED_BINARY_PATH, args: ['--disable-gpu', '--no-sandbox'], env })
    const page = await app.firstWindow()
    installErrorBannerGuard(page)

    return { app, page }
  }

  let client = await launch()

  const send = async (page: Page, text: string) => {
    const composer = page.locator('[contenteditable="true"]:visible').first()
    await composer.click()
    await composer.type(text, { delay: 10 })
    await page.keyboard.press('Enter')
  }

  const transcript = (page: Page) => page.locator('[data-slot="aui_thread-viewport"]:visible').last()
  const dbRows = () => JSON.parse(execFileSync('sqlite3', ['-json', path.join(sandbox.hermesHome, 'state.db'), "select role,content,display_kind from messages order by id"], { encoding: 'utf8' })) as Array<{ role: string; content: string; display_kind: string }>

  try {
    await waitForAppReady({ ...client, sandbox, cleanup: async () => {} }, 120_000)
    await send(client.page, SILENT)
    await expect.poll(() => prefixSent, { timeout: 60_000 }).toBe(true)
    await expect(client.page.getByText(TASK, { exact: true }).first()).toBeVisible({ timeout: 15_000 })
    // Prefix remains held at inference: inspect a real streaming UI, not only its final DOM.
    await expect(transcript(client.page).locator('[data-role="assistant"]')).not.toContainText('NO_')
    await client.page.screenshot({ path: testInfo.outputPath('streaming-prefix-and-native-tasks.png') })
    pending.splice(0).forEach(release => release())
    await expect.poll(() => dbRows().some(row => row.role === 'assistant' && row.content === 'NO_REPLY' && row.display_kind === 'intentional_silence'), { timeout: 30_000 }).toBe(true)
    await expect(transcript(client.page).locator('[data-role="assistant"]')).not.toContainText('NO_REPLY')
    expect(requests.some(request => request.messages.some(message => message.role === 'tool'))).toBe(true)
    // A new real turn proves the previous silent completion settled the lifecycle.
    await send(client.page, NORMAL)
    await expect(transcript(client.page)).toContainText(VISIBLE, { timeout: 60_000 })
    expect(requests.some(request => request.messages.some(message => message.role === 'assistant' && message.content === 'NO_REPLY'))).toBe(true)
    await client.page.screenshot({ path: testInfo.outputPath('normal-content-preserved.png') })
    await client.app.close()
    client = await launch()
    await waitForAppReady({ ...client, sandbox, cleanup: async () => {} }, 120_000)
    const row = client.page.locator('[data-slot="sidebar"] button').filter({ hasText: SILENT }).first()
    await row.click()
    await expect(transcript(client.page)).toContainText(VISIBLE, { timeout: 30_000 })
    const assistantTexts = await transcript(client.page).locator('[data-role="assistant"]').allTextContents()
    expect(assistantTexts.some(text => text.trim() === 'NO_REPLY')).toBe(false)
    expect(dbRows().some(row => row.role === 'tool')).toBe(true)
    await client.page.screenshot({ path: testInfo.outputPath('cold-reopened-history.png') })
    allowErrorBanners()
    await send(client.page, FAILURE)
    await expect(client.page.locator('body')).toContainText('E2E deliberate provider failure', { timeout: 60_000 })
    await client.page.screenshot({ path: testInfo.outputPath('visible-error-control.png') })
    await testInfo.attach('real-chain-evidence', { body: JSON.stringify({ packaged, binary: packaged ? PACKAGED_BINARY_PATH : 'source Electron', inference: 'controlled fixture', rows: dbRows(), receivedRequests: requests.length }, null, 2), contentType: 'application/json' })
  } finally {
    pending.splice(0).forEach(release => release())
    await testInfo.attach('inference-requests', { contentType: 'application/json', body: JSON.stringify(requests.map(request => ({
      stream: request.stream,
      messages: request.messages.filter(message => message.role !== 'system'),
    })), null, 2) })
    await testInfo.attach('ui-dom', { contentType: 'text/plain', body: await client.page.locator('body').innerText() })
    await client.app.close().catch(() => undefined)
    server.closeAllConnections()
    await new Promise<void>(resolve => server.close(() => resolve()))
    sandbox.cleanup()
  }
})
