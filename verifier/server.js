'use strict'
// Worldless, online-mode-only Java identity verifier. No offline UUIDs accepted.
const mc = require('minecraft-protocol')
const nbt = require('prismarine-nbt')
const { tokenFromHost, verifiedProfile } = require('./validation')

function start ({ suffix = process.env.MC_VERIFY_DOMAIN, secret = process.env.VERIFY_SHARED_SECRET,
  port = Number(process.env.PORT || 25565), host = '0.0.0.0',
  callback = process.env.VERIFY_CALLBACK || 'http://bot:8080/verify' } = {}) {
  if (!suffix || !secret || secret.length < 32) throw new Error('Verifier domain and shared secret must be configured')
  const server = mc.createServer({
    host, port, version: false, 'online-mode': true,
    motd: 'Giveaway Minecraft連携 / Discordから専用アドレスを発行してください',
    maxPlayers: 16, keepAlive: false, kickTimeout: 15000, hideErrors: true,
    errorHandler: client => client.socket.destroy()
  })
  server.socketServer.maxConnections = 16
  const rates = new Map()
  server.on('connection', client => {
    const now = Date.now()
    for (const [ip, v] of rates) if (v.until < now) rates.delete(ip)
    const ip = client.socket.remoteAddress
    const rate = rates.get(ip) || { count: 0, until: now + 60000 }
    rates.set(ip, rate)
    if (++rate.count > 15 || rates.size > 1000) return client.socket.destroy()
    client.socket.setTimeout(20000, () => client.socket.destroy())
    client.once('set_protocol', packet => {
      if (packet.nextState === 2 && !tokenFromHost(packet.serverHost, suffix)) {
        client.socket.destroy()
      }
    })
  })
  server.on('login', async client => {
    // Identity verification needs no world registries. Stop the normal login
    // plugin at CONFIGURATION, before its version-default registry broadcast.
    if (client.supportFeature('hasConfigurationState')) {
      client.removeAllListeners('login_acknowledged')
      client.once('login_acknowledged', () => { client.state = 'configuration' })
    }
    async function finish (message) {
      if (client.supportFeature('hasConfigurationState')) {
        if (client.state === 'login') {
          await new Promise(resolve => {
            const timer = setTimeout(done, 2000)
            function done () {
              clearTimeout(timer)
              client.removeListener('state', changed)
              client.removeListener('end', done)
              resolve()
            }
            function changed (state) { if (state === 'configuration') done() }
            client.on('state', changed)
            client.once('end', done)
          })
        }
        if (client.state !== 'configuration') return client.socket.destroy()
        const reason = client.supportFeature('chatPacketsUseNbtComponents')
          ? nbt.comp({ text: nbt.string(message) }) : JSON.stringify({ text: message })
        client.write('disconnect', { reason })
        client._end(message)
      } else {
        client.end(message)
      }
    }
    const token = tokenFromHost(client.serverHost, suffix)
    const profile = verifiedProfile(client)
    if (!token || !profile) return client.end('正規アカウントの本人確認が必要です。')
    try {
      const response = await fetch(callback, {
        method: 'POST', headers: { Authorization: `Bearer ${secret}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ token, ...profile }), signal: AbortSignal.timeout(8000)
      })
      const result = await response.json()
      const message = response.ok && result.ok ? 'Minecraft連携が完了しました！Discordに戻ってください。' :
        (result.message || '連携できませんでした。Discordからやり直してください。')
      await finish(message)
    } catch {
      await finish('連携結果を確認できません。Discordの /minecraft status を確認してください。')
    }
  })
  server.on('error', error => console.error('Verifier listener error:', error.code || error.name))
  server.on('listening', () => console.log('Online-mode Minecraft verifier listening'))
  return server
}

module.exports = { start }
if (require.main === module) {
  const server = start()
  process.on('SIGTERM', () => { server.close(); setTimeout(() => process.exit(0), 1000).unref() })
}
