const { test } = require('node:test')
const assert = require('node:assert/strict')
const http = require('node:http')
const { once } = require('node:events')
const mc = require('minecraft-protocol')
const yggdrasil = require('yggdrasil')
const { start } = require('../server')
const { tokenFromHost, verifiedProfile } = require('../validation')

test('only exact one-use hostname form is accepted', () => {
  const token = 'a'.repeat(32)
  assert.equal(tokenFromHost(token + '.verify.example.com', 'verify.example.com'), token)
  for (const host of ['verify.example.com', token + '.evilverify.example.com', token + '.verify.example.com.evil', token + '.verify.example.com\0spoof', 'a.b.verify.example.com']) {
    assert.equal(tokenFromHost(host, 'verify.example.com'), null)
  }
})

test('claimed UUID without sessionserver profile is rejected', () => {
  assert.equal(verifiedProfile({ username: 'Steve', uuid: 'a'.repeat(32) }), null)
})

test('real protocol login reaches callback only after session authentication', { timeout: 30000 }, async () => {
  const original = yggdrasil.server
  let authenticated = true
  // Test-only mock at Mojang's external session service boundary. RSA/encrypted login remains real.
  yggdrasil.server = () => ({ hasJoined: (name, sid, secret, pubkey, cb) => {
    if (authenticated) cb(null, { id: 'a'.repeat(32), name })
    else cb(new Error('not joined'))
  } })
  const calls = []
  const callback = http.createServer(async (req, res) => {
    let data = ''
    for await (const part of req) data += part
    calls.push({ body: JSON.parse(data), authorization: req.headers.authorization })
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify({ ok: true }))
  }).listen(0, '127.0.0.1')
  await once(callback, 'listening')
  const server = start({ port: 0, host: '127.0.0.1', suffix: 'verify.example.com', secret: 's'.repeat(40), callback: `http://127.0.0.1:${callback.address().port}` })
  await once(server, 'listening')
  async function login (version, hostname = 'b'.repeat(32) + '.verify.example.com') {
    const client = mc.createClient({ host: '127.0.0.1', port: server.socketServer.address().port, fakeHost: hostname, username: 'Steve', auth: 'offline', version })
    const registries = []
    const disconnects = []
    client.on('disconnect', packet => disconnects.push(packet))
    client.on('kick_disconnect', packet => disconnects.push(packet))
    client.on('registry_data', packet => registries.push(packet))
    client.on('error', () => {})
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { client.end(); reject(new Error('protocol timeout')) }, 6000)
      client.once('end', () => { clearTimeout(timer); resolve() })
    })
    if (authenticated && hostname.startsWith('b'.repeat(32))) {
      assert.equal(registries.length, 0, 'identity-only server must not send world registries')
      assert.ok(JSON.stringify(disconnects).includes('Minecraft連携が完了しました'), 'client must decode the success disconnect')
    }
  }
  try {
    for (const version of ['1.20.4', '1.21.11', '26.1']) await login(version)
    assert.equal(calls.length, 3)
    assert.equal(calls[0].body.uuid, 'a'.repeat(32))
    assert.equal(calls[0].body.token, 'b'.repeat(32))
    assert.equal(calls[0].authorization, 'Bearer ' + 's'.repeat(40))
    authenticated = false
    await login('1.20.4')
    assert.equal(calls.length, 3, 'failed online authentication must never link')
    await login('1.20.4', 'wrong.example.com')
    assert.equal(calls.length, 3, 'wrong hostname must never link')
  } finally {
    server.close()
    callback.closeAllConnections()
    callback.close()
    yggdrasil.server = original
  }
})
