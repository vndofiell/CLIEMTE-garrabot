const express = require('express');
const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode');
const app = express();
app.use(express.json());

let client = null;
let qrData = null;
let status = 'DISCONNECTED';

function iniciarCliente() {
    const chromiumExec =
        require('fs').existsSync('/usr/bin/chromium-browser') ? '/usr/bin/chromium-browser' :
        require('fs').existsSync('/usr/bin/chromium')         ? '/usr/bin/chromium' :
        undefined;

    client = new Client({
        authStrategy: new LocalAuth({ clientId: 'GarraBot' }),
        webVersionCache: {
            type: 'remote',
            remotePath: 'https://raw.githubusercontent.com/wppconnect-team/wa-version/main/html/2.3000.1023054362-alpha.html'
        },
        puppeteer: {
            executablePath: chromiumExec,
            args: [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--no-first-run',
                '--no-zygote',
                '--single-process'
            ]
        }
    });

    client.on('qr', qr => {
        qrData = qr;
        status = 'QR_READY';
        console.log('[WA] QR gerado — escaneie pelo painel do bot');
    });
    client.on('ready', () => {
        status = 'CONNECTED';
        qrData = null;
        console.log('[WA] Conectado!');
    });
    client.on('disconnected', reason => {
        status = 'DISCONNECTED';
        console.log('[WA] Desconectado:', reason);
        setTimeout(iniciarCliente, 5000);
    });
    client.initialize();
}
iniciarCliente();

app.get('/ping', (req, res) => res.json({ ok: true, status }));

app.get('/session/status/:id', (req, res) => res.json({ state: status, success: true }));

app.get('/session/qr/:id', (req, res) => {
    if (status === 'CONNECTED') return res.json({ state: 'CONNECTED', success: true });
    if (!qrData) return res.json({ success: false, state: status });
    res.json({ success: true, qr: qrData });
});

app.get('/session/start/:id', (req, res) => {
    console.log('[WA] /session/start chamado');
    res.json({ ok: true, state: status });
});

app.post('/client/sendMessage/:id', async (req, res) => {
    const { chatId, content } = req.body;
    if (status !== 'CONNECTED') {
        return res.status(503).json({ ok: false, erro: 'WhatsApp nao conectado. Estado: ' + status });
    }
    if (!chatId || !content) {
        return res.status(400).json({ ok: false, erro: 'chatId e content sao obrigatorios' });
    }
    try {
        await client.sendMessage(chatId, content);
        console.log('[WA] Mensagem enviada para', chatId);
        res.json({ ok: true });
    } catch(e) {
        console.error('[WA] Erro ao enviar:', e.message);
        res.status(500).json({ ok: false, erro: e.message });
    }
});

app.get('/session/terminate/:id', async (req, res) => {
    try {
        if (client) await client.destroy();
        status = 'DISCONNECTED';
        res.json({ ok: true });
    } catch(e) {
        res.json({ ok: false, erro: e.message });
    }
});

app.listen(3000, () => console.log('[WA] API GarraBot rodando na porta 3000'));
