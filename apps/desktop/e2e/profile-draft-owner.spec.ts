import * as fs from 'node:fs'
import * as path from 'node:path'
import {
  buildAppEnv,
  createSandbox,
  launchDesktop,
  waitForAppReady,
  writeEnvFile,
  writeMockProviderConfig
} from './fixtures'
import { MOCK_REPLY, startMockServer } from '../../../tests-js/scripts/mock-server'
import { expect, test } from './test'

test('a profile draft can read controls and send its first message', async () => {
  test.setTimeout(120_000)
  const sandbox = createSandbox('profile-draft-owner')
  const mock = await startMockServer()
  const profileHome = path.join(sandbox.hermesHome, 'profiles', 'personal')
  fs.mkdirSync(profileHome, { recursive: true })
  for (const home of [sandbox.hermesHome, profileHome]) {
    writeMockProviderConfig(home, mock.url)
    writeEnvFile(home)
  }
  const { app, page } = await launchDesktop(
    buildAppEnv(sandbox, { HERMES_HOME: profileHome, HERMES_PROFILE: 'personal' })
  )
  try {
    const requests = new Map<string, { method: string; params?: { session_id?: string } }>()
    let createdRuntime = ''
    let controlsRead = false
    page.on('websocket', socket => {
      socket.on('framesent', frame => {
        const message = JSON.parse(String(frame.payload))
        if (message.id && message.method) requests.set(String(message.id), message)
      })
      socket.on('framereceived', frame => {
        const message = JSON.parse(String(frame.payload))
        const request = requests.get(String(message.id))
        if (request?.method === 'session.create') createdRuntime = message.result?.session_id || ''
        if (request?.method === 'session.control.read' && request.params?.session_id === createdRuntime) {
          controlsRead = Boolean(message.result?.control) && !message.error
        }
      })
    })
    await waitForAppReady({ app, page, sandbox } as never)
    await page.getByRole('button', { name: 'personal', exact: true }).click()
    await page.keyboard.press(process.platform === 'darwin' ? 'Meta+t' : 'Control+t')
    await expect.poll(() => createdRuntime).not.toBe('')
    await expect.poll(() => controlsRead).toBe(true)
    await expect(page.getByText(/Session controls unavailable/)).toHaveCount(0)
    const composer = page.locator('textarea, [contenteditable="true"]').filter({ visible: true }).first()
    await composer.fill('Reply with the draft verification response.')
    await composer.press('Enter')
    await expect(page.getByText(/Session controls unavailable/)).toHaveCount(0)
    await expect(page.locator('[role="alert"]')).toHaveCount(0)
    await expect(page.getByText(MOCK_REPLY, { exact: false }).first()).toBeVisible({ timeout: 45_000 })
  } finally {
    await app.close()
    await mock.close()
    sandbox.cleanup()
  }
})
