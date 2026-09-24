// ═══════════════════════════════════════════════════════════════════════════════
// GARRABOT — Quotex Sync Banca  v1.4
// Script de injeção para sincronizar o saldo da Quotex com o servidor GarraBot.
//
// USO (escolha um dos métodos abaixo):
//
//  Método A — Tampermonkey / Greasemonkey (RECOMENDADO):
//    1. Instale o Tampermonkey no Chrome: https://www.tampermonkey.net/
//    2. Clique em "Criar novo script"
//    3. Cole TODO o conteúdo deste arquivo e salve (Ctrl+S)
//    4. Abra a Quotex — o script inicia automaticamente
//
//    OU instale direto pela URL do servidor:
//    Tampermonkey → Dashboard → Utilitários → "Instalar do URL":
//      https://garrabot.duckdns.org/quotex/sync-script
//
//  Método B — Console do navegador (teste rápido, sem instalar nada):
//    1. Abra a Quotex no Chrome
//    2. Pressione F12 → aba "Console"
//    3. Cole todo o conteúdo abaixo e pressione Enter
//
//  NOVA FUNCIONALIDADE v1.4 — Detecção automática de resultado de trade:
//    O script agora observa o DOM da Quotex e detecta quando um trade fecha
//    (WIN ou LOSS), extraindo o lucro/prejuízo e o novo saldo, e envia tudo
//    para o servidor GarraBot via POST /quotex/notificar-resultado.
//    O servidor então dispara a notificação Telegram automaticamente.
//
//  FUNCIONALIDADE v1.3 — Alinhamento de entrada à virada do minuto:
//    O script expõe window.garrabot.esperarViradaMinuto(cb, antecipacaoMs)
//    que adia a execução de qualquer callback até exatamente N ms ANTES da
//    virada do minuto.
//
// CONFIGURAÇÃO:
//   Altere GARRABOT_URL para 'http://localhost:5000' se testar localmente.
// ═══════════════════════════════════════════════════════════════════════════════

// ==UserScript==
// @name         GarraBot — Sync Banca Quotex
// @namespace    garrabot
// @version      1.5
// @description  Sincroniza saldo + detecta resultados de trades + alinha entradas à virada do minuto
// @author       GarraBot
// @match        https://quotex.com/*
// @match        https://broker.quotex.com/*
// @match        https://qxbroker.com/*
// @match        https://*.quotex.io/*
// @match        https://*.qxbroker.com/*
// @grant        GM_xmlhttpRequest
// @connect      localhost
// @connect      127.0.0.1
// @connect      garrabot.duckdns.org
// ==/UserScript==

(function () {
    'use strict';

    // ════════════════════════════════════════════
    // CONFIGURAÇÃO — edite aqui se necessário
    // ════════════════════════════════════════════
    //
    // Auto-detecta se o bot está rodando localmente (127.0.0.1:5000)
    // ou no servidor externo. Para forçar um endereço, substitua por:
    //   const GARRABOT_URL = 'http://localhost:5000';
    //   const GARRABOT_URL = 'https://garrabot.duckdns.org';
    const GARRABOT_URL  = (function() {
        // Testa se o servidor local responde; enquanto isso usa o configurado
        // A detecção real acontece no _init via _testarServidores()
        return 'http://127.0.0.1:5000';
    })();
    const GARRABOT_URL_FALLBACK = 'https://garrabot.duckdns.org';
    const INTERVALO_MS  = 5000;   // polling de segurança: verifica saldo a cada 5 segundos
    const DEBOUNCE_MS   = 400;    // agrupa chamadas do MutationObserver (evita spam)
    const DEBUG         = true;   // logs no console — mude para false em produção

    // ════════════════════════════════════════════
    // ALINHAMENTO À VIRADA DO MINUTO
    // ════════════════════════════════════════════
    const ANTECIPACAO_MS = 3000;  // entra 3 segundos antes da virada (editável)
    const AVISO_MS       = 8000;  // loga aviso no console quando faltam 8s para a virada

    let _timerVirada    = null;
    let _aguardandoVirada = false;

    function _msAteVirada() {
        const agora   = Date.now();
        const msNoMin = agora % 60000;
        return 60000 - msNoMin;
    }

    function esperarViradaMinuto(callback, antecipMs, minutos) {
        antecipMs = (antecipMs !== undefined) ? antecipMs : ANTECIPACAO_MS;
        minutos   = (minutos   !== undefined) ? minutos   : 0;

        if (_aguardandoVirada) {
            console.warn('[GarraBot] ⚠️ Já existe uma entrada agendada. Ignorando nova solicitação.');
            return;
        }

        const msFalta = _msAteVirada();
        const atraso  = msFalta + (minutos * 60000) - antecipMs;

        if (atraso <= 0) {
            console.log(`[GarraBot] ⚡ Já na janela de entrada (${Math.abs(atraso)}ms dentro). Entrando agora.`);
            callback();
            return;
        }

        _aguardandoVirada = true;
        console.log(`[GarraBot] ⏳ Entrada agendada em ${(atraso / 1000).toFixed(1)}s (${antecipMs / 1000}s antes da virada do minuto ${minutos > 0 ? '+ ' + minutos + 'min' : ''}).`);

        const atrasoAviso = atraso - (AVISO_MS - antecipMs);
        if (atrasoAviso > 0) {
            setTimeout(() => {
                if (_aguardandoVirada) {
                    console.log(`[GarraBot] 🔔 Faltam ${antecipMs / 1000}s para a entrada!`);
                }
            }, atrasoAviso);
        }

        _timerVirada = setTimeout(() => {
            _aguardandoVirada = false;
            _timerVirada      = null;
            console.log(`[GarraBot] ✅ VIRADA DO MINUTO — executando entrada agora!`);
            try {
                callback();
            } catch (e) {
                console.error('[GarraBot] ❌ Erro ao executar callback da entrada:', e);
            }
        }, atraso);
    }

    function cancelarVirada() {
        if (_timerVirada) {
            clearTimeout(_timerVirada);
            _timerVirada = null;
        }
        _aguardandoVirada = false;
        console.log('[GarraBot] 🚫 Entrada cancelada.');
    }

    function entrarNaVirada(direcao, onEnter, minutos) {
        const dir = (direcao || 'call').toLowerCase();

        const SELETORES_CALL = [
            '[class*="call-btn"]',
            '[class*="up-btn"]',
            '[class*="higher"]',
            'button[class*="call"]',
            'button:not([disabled])[class*="btn"]',
        ];
        const SELETORES_PUT = [
            '[class*="put-btn"]',
            '[class*="down-btn"]',
            '[class*="lower"]',
            'button[class*="put"]',
        ];

        function _clicarBotao() {
            const seletores = dir === 'call' ? SELETORES_CALL : SELETORES_PUT;
            let clicado = false;

            for (const sel of seletores) {
                try {
                    const btns = document.querySelectorAll(sel);
                    for (const btn of btns) {
                        const txt = (btn.textContent || '').toLowerCase();
                        const ehCall = dir === 'call' && (txt.includes('cima') || txt.includes('call') || txt.includes('up') || txt.includes('higher'));
                        const ehPut  = dir === 'put'  && (txt.includes('baixo') || txt.includes('put') || txt.includes('down') || txt.includes('lower'));
                        if (ehCall || ehPut) {
                            btn.click();
                            clicado = true;
                            console.log(`[GarraBot] 🟢 Botão ${dir.toUpperCase()} clicado: "${btn.textContent.trim()}"`);
                            break;
                        }
                    }
                    if (clicado) break;
                } catch (_) {}
            }

            if (!clicado) {
                console.warn(`[GarraBot] ⚠️ Botão ${dir.toUpperCase()} não encontrado no DOM.`);
            }

            if (typeof onEnter === 'function') {
                try { onEnter(clicado); } catch (_) {}
            }
        }

        esperarViradaMinuto(_clicarBotao, ANTECIPACAO_MS, minutos || 0);
    }

    // ════════════════════════════════════════════
    // SELETORES DO DOM DA QUOTEX — SALDO
    //
    // Ordem de prioridade: do mais específico ao mais genérico.
    // O saldo "Saldo: $188.00" aparece no canto superior direito.
    // Inspecione com F12 → clique no número do saldo → copie o seletor.
    // ════════════════════════════════════════════
    const SELETORES_SALDO = [
        // ── qxbroker.com — seletores reais observados no DOM ──────────────────
        // O elemento "CONTA DEMO $100.93" fica no cabeçalho
        '.header-balance',              // container do saldo no header
        '.header-balance .value',       // só o número dentro do container
        '.header-balance__value',
        '.header-balance span',
        // Variações de classe Vue/React (nomes gerados por hash podem mudar)
        '[class*="header-balance"]',
        '[class*="headerBalance"]',
        '[class*="header_balance"]',
        // Elemento do painel lateral injetado (Saldo: $xx)
        '.trading-panel__balance-value',
        '.trading-panel .balance',
        // Seletores genéricos BEM
        '.balance__value',
        '.balance-value',
        '.balance-text',
        // Seletor do widget de conta no canto superior direito
        '.account-info__balance',
        '.account-info .balance',
        '.user-info__balance',
        '[class*="account-info"][class*="balance"]',
        // Outros seletores da Quotex
        '.trading__balance-value',
        '.deal-amount__balance',
        '[data-type="balance"]',
        '[data-balance]',
        '[class*="balance__value"]',
        '[class*="balance-value"]',
        '[class*="balance_value"]',
        '[class*="balance__amount"]',
        '.trading-panel__balance',
        '.user-balance',
        '.account-balance',
        '.header__balance-value',
        '.header__balance .value',
        '.account__balance-value',
        '.account-balance__value',
        '.trading__header-balance',
        '.balance-info__value',
        '.balance-info .value',
    ];

    // ════════════════════════════════════════════
    // SELETORES DO DOM DA QUOTEX — RESULTADOS
    //
    // A Quotex exibe um popup/banner de resultado após cada trade.
    // Mapeamos os seletores mais comuns encontrados no DOM real.
    // Se o seu broker exibe elementos diferentes, adicione aqui.
    // ════════════════════════════════════════════
    const SELETORES_RESULTADO = {
        // Container principal do resultado (exibido após fechamento do trade)
        // qxbroker.com mostra "WIN! +$0.93" ou "LOSS -$1.00" em um banner verde/vermelho
        container: [
            // qxbroker.com — banner de resultado (verde = win, vermelho = loss)
            '.deals-result',
            '.deal-result',
            '.trade-result',
            '.trading-result',
            '.result-popup',
            '.result-notification',
            '.notification--win',
            '.notification--loss',
            '.notification',
            // Vue/React hashed classnames
            '[class*="deal-result"]',
            '[class*="deals-result"]',
            '[class*="trade-result"]',
            '[class*="result-popup"]',
            '[class*="tradeResult"]',
            '[class*="dealResult"]',
            '[class*="notification--win"]',
            '[class*="notification--loss"]',
        ],
        // Elemento que indica WIN
        win: [
            '.deals-result--win',
            '.deal-result--win',
            '.trade-result--win',
            '.notification--win',
            '[class*="result--win"]',
            '[class*="result-win"]',
            '[class*="notification--win"]',
            '[class*="--win"]',
        ],
        // Elemento que indica LOSS
        loss: [
            '.deals-result--loss',
            '.deal-result--loss',
            '.trade-result--loss',
            '.notification--loss',
            '[class*="result--loss"]',
            '[class*="result-loss"]',
            '[class*="notification--loss"]',
            '[class*="--loss"]',
        ],
        // Valor monetário do resultado (ex: "+$12.50" ou "-$5.00")
        valor: [
            '.deals-result__profit',
            '.deal-result__profit',
            '.trade-result__amount',
            '.result-profit',
            '.profit-amount',
            '[class*="result__profit"]',
            '[class*="result__amount"]',
            '[class*="profit-amount"]',
            '[class*="deal-profit"]',
        ],
        // Ativo negociado (ex: "EUR/USD OTC")
        ativo: [
            '.deals-result__asset',
            '.deal-result__asset',
            '.result-asset',
            '[class*="result__asset"]',
            '[class*="result-asset"]',
        ],
        // Direção (CALL / PUT)
        direcao: [
            '.deals-result__direction',
            '.deal-result__direction',
            '[class*="result__direction"]',
            '[class*="deal-type"]',
            '[class*="direction"]',
        ],
    };

    // ════════════════════════════════════════════
    // ESTADO INTERNO — SALDO
    // ════════════════════════════════════════════
    let _ultimoSaldo    = null;
    let _intervaloId    = null;
    let _observador     = null;
    let _debounceTimer  = null;

    // ════════════════════════════════════════════
    // ESTADO INTERNO — RESULTADO
    // ════════════════════════════════════════════
    let _ultimoResultadoHash = null;   // evita enviar o mesmo resultado duas vezes
    let _wins         = 0;
    let _losses       = 0;
    let _profitTotal  = 0;
    let _ultimaStake  = 5.0;           // stake padrão; atualizada quando detectada
    let _estrategia   = 'Quotex';      // pode ser sobrescrita via window.garrabot.setEstrategia()
    let _modo         = 'demo';        // 'demo' | 'real'; detectado automaticamente

    // ════════════════════════════════════════════
    // UTILITÁRIOS DE PARSING
    // ════════════════════════════════════════════

    function _parseSaldo(texto) {
        if (!texto) return null;
        let limpo = texto.replace(/[^0-9.,]/g, '');
        if (!limpo) return null;
        const temPontoEVirgula = limpo.includes('.') && limpo.includes(',');
        if (temPontoEVirgula) {
            const ultimoPonto   = limpo.lastIndexOf('.');
            const ultimaVirgula = limpo.lastIndexOf(',');
            if (ultimaVirgula > ultimoPonto) {
                limpo = limpo.replace(/\./g, '').replace(',', '.');
            } else {
                limpo = limpo.replace(/,/g, '');
            }
        } else {
            limpo = limpo.replace(',', '.');
        }
        const valor = parseFloat(limpo);
        return isNaN(valor) || valor <= 0 ? null : valor;
    }

    /** Extrai número positivo ou negativo de uma string como "+$12.50" ou "- 5,00" */
    function _parseValorMonetario(texto) {
        if (!texto) return null;
        const negativo = texto.includes('-');
        let limpo = texto.replace(/[^0-9.,]/g, '');
        if (!limpo) return null;
        const temPontoEVirgula = limpo.includes('.') && limpo.includes(',');
        if (temPontoEVirgula) {
            const ultimoPonto   = limpo.lastIndexOf('.');
            const ultimaVirgula = limpo.lastIndexOf(',');
            if (ultimaVirgula > ultimoPonto) {
                limpo = limpo.replace(/\./g, '').replace(',', '.');
            } else {
                limpo = limpo.replace(/,/g, '');
            }
        } else {
            limpo = limpo.replace(',', '.');
        }
        const valor = parseFloat(limpo);
        if (isNaN(valor)) return null;
        return negativo ? -Math.abs(valor) : Math.abs(valor);
    }

    /** Tenta ler texto de qualquer seletor de uma lista */
    function _lerTextoSeletor(seletores, contexto) {
        const root = contexto || document;
        for (const sel of seletores) {
            try {
                const el = root.querySelector(sel);
                if (el) {
                    const t = el.textContent.trim();
                    if (t) return t;
                }
            } catch (_) {}
        }
        return null;
    }

    /** Testa se algum seletor de uma lista existe dentro de um contexto */
    function _existeNoContexto(seletores, contexto) {
        for (const sel of seletores) {
            try {
                if (contexto.querySelector(sel)) return true;
            } catch (_) {}
        }
        return false;
    }

    // ════════════════════════════════════════════
    // SINCRONIZAÇÃO DE SALDO
    // ════════════════════════════════════════════

    function _encontrarElementoSaldo() {
        for (const seletor of SELETORES_SALDO) {
            try {
                const el = document.querySelector(seletor);
                if (el && el.textContent.trim()) return el;
            } catch (_) {}
        }
        return null;
    }

    function _lerSaldo() {
        const el = _encontrarElementoSaldo();
        if (!el) {
            // Fallback genérico: procura qualquer elemento folha visível
            // cujo texto seja exatamente um número no formato "$xxx.xx"
            try {
                const todos = document.querySelectorAll('[class]');
                for (const node of todos) {
                    if (node.children.length > 0) continue; // só nós folha
                    const txt = node.textContent.trim();
                    const match = txt.match(/^\$?([0-9]{1,10}[.,][0-9]{2})$/);
                    if (match && node.offsetParent !== null) {
                        const val = _parseSaldo(txt);
                        if (val && val > 0) {
                            if (DEBUG) console.log(`[GarraBot] 🔍 Saldo via fallback genérico: "${txt}" (${node.className})`);
                            return val;
                        }
                    }
                }
            } catch (_) {}
            return null;
        }
        return _parseSaldo(el.textContent.trim());
    }

    /**
     * Diagnóstico: imprime no console todos os elementos com "balance" no DOM.
     * Execute no console da Quotex: window.garrabot.diagnosticarSaldo()
     * Copie a classe do elemento que mostra o saldo e adicione em SELETORES_SALDO.
     */
    function _diagnosticarSaldo() {
        console.log('[GarraBot] 🔍 Diagnóstico — buscando elementos "balance" no DOM...');
        const candidatos = [];
        document.querySelectorAll('[class*="balance"], [id*="balance"], [data-balance]').forEach(el => {
            const txt = el.textContent.trim().substring(0, 60);
            if (txt) candidatos.push({ tag: el.tagName, class: el.className, id: el.id, texto: txt });
        });
        if (candidatos.length === 0) {
            console.warn('[GarraBot] ⚠️ Nenhum elemento "balance" encontrado. Use F12 → inspecionar o número do saldo.');
        } else {
            console.table(candidatos);
            console.log('[GarraBot] 💡 Adicione o seletor correto em SELETORES_SALDO no script.');
        }
        return candidatos;
    }

    // URL ativa — começa com local, troca para externo se falhar
    let _servidorAtivo = GARRABOT_URL;

    /** Envia POST para um endpoint, tentando local primeiro e depois externo. */
    function _postComFallback(endpoint, corpo, onSucesso) {
        const tentarUrl = (url, ehFallback) => {
            if (typeof GM_xmlhttpRequest === 'function') {
                GM_xmlhttpRequest({
                    method:  'POST',
                    url:     url + endpoint,
                    headers: { 'Content-Type': 'application/json' },
                    data:    corpo,
                    onload:  (r) => {
                        _servidorAtivo = url;
                        if (DEBUG) console.log(`[GarraBot] ✅ ${url}${endpoint} →`, r.status, r.responseText.substring(0,80));
                        if (typeof onSucesso === 'function') {
                            try { onSucesso(JSON.parse(r.responseText)); } catch(_) {}
                        }
                    },
                    onerror: () => {
                        if (!ehFallback && GARRABOT_URL_FALLBACK && url !== GARRABOT_URL_FALLBACK) {
                            console.warn(`[GarraBot] ⚠️ ${url}${endpoint} falhou. Tentando fallback: ${GARRABOT_URL_FALLBACK}`);
                            tentarUrl(GARRABOT_URL_FALLBACK, true);
                        } else {
                            console.error(`[GarraBot] ❌ Falha em todos os servidores para ${endpoint}`);
                        }
                    },
                });
            } else {
                fetch(url + endpoint, {
                    method:  'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body:    corpo,
                })
                .then(r => { _servidorAtivo = url; return r.json(); })
                .then(d  => {
                    if (DEBUG) console.log(`[GarraBot] ✅ ${url}${endpoint}:`, d);
                    if (typeof onSucesso === 'function') onSucesso(d);
                })
                .catch(() => {
                    if (!ehFallback && GARRABOT_URL_FALLBACK && url !== GARRABOT_URL_FALLBACK) {
                        console.warn(`[GarraBot] ⚠️ ${url}${endpoint} falhou. Tentando fallback.`);
                        tentarUrl(GARRABOT_URL_FALLBACK, true);
                    } else {
                        console.error(`[GarraBot] ❌ Falha em todos os servidores para ${endpoint}`);
                    }
                });
            }
        };
        tentarUrl(_servidorAtivo, false);
    }

    function _enviarSaldo(saldo) {
        const payload = JSON.stringify({ saldo });
        console.log(`[GarraBot] 📤 Enviando saldo: $${saldo} → ${_servidorAtivo}`);
        _postComFallback('/quotex/sincronizar-saldo', payload);
        _postComFallback('/jc-sec-sincronizar',       payload);
    }

    function _verificarESincronizar() {
        const saldo = _lerSaldo();
        if (saldo === null) return;
        if (saldo === _ultimoSaldo) return;
        _ultimoSaldo = saldo;
        _enviarSaldo(saldo);
    }

    function _verificarComDebounce() {
        clearTimeout(_debounceTimer);
        _debounceTimer = setTimeout(_verificarESincronizar, DEBOUNCE_MS);
    }

    // ════════════════════════════════════════════
    // DETECÇÃO DE RESULTADO DE TRADE  (v1.4 NEW)
    // ════════════════════════════════════════════

    /**
     * Detecta automaticamente se estamos em conta DEMO ou REAL
     * observando o DOM da Quotex.
     */
    function _detectarModo() {
        try {
            const textoBody = document.body.innerText.toLowerCase();
            // Indicadores comuns de conta DEMO
            if (
                document.querySelector('[class*="demo"]') ||
                document.querySelector('[class*="practice"]') ||
                textoBody.includes('demo account') ||
                textoBody.includes('conta demo') ||
                textoBody.includes('practice')
            ) {
                return 'demo';
            }
        } catch (_) {}
        return 'real';
    }

    /**
     * Tenta encontrar um container de resultado recém-aparecido.
     * Retorna o elemento ou null.
     */
    function _encontrarContainerResultado() {
        for (const sel of SELETORES_RESULTADO.container) {
            try {
                const els = document.querySelectorAll(sel);
                for (const el of els) {
                    // Só considera elementos visíveis
                    if (el.offsetParent !== null || el.style.display !== 'none') {
                        return el;
                    }
                }
            } catch (_) {}
        }
        return null;
    }

    /**
     * Lê todos os dados possíveis de um container de resultado e
     * envia para o GarraBot via POST /quotex/notificar-resultado.
     */
    function _processarResultado(container) {
        // Determina WIN ou LOSS
        const ehWin  = _existeNoContexto(SELETORES_RESULTADO.win,  container);
        const ehLoss = _existeNoContexto(SELETORES_RESULTADO.loss, container);

        // Fallback: tenta pelo texto geral do container
        let win = false;
        if (ehWin && !ehLoss) {
            win = true;
        } else if (!ehWin && ehLoss) {
            win = false;
        } else {
            // Nenhum seletor específico bateu — tenta via texto
            const textoContainer = (container.textContent || '').toLowerCase();
            if (textoContainer.includes('win') || textoContainer.includes('profit') ||
                textoContainer.includes('ganhou') || textoContainer.includes('ganho')) {
                win = true;
            } else if (textoContainer.includes('loss') || textoContainer.includes('lost') ||
                       textoContainer.includes('perdeu') || textoContainer.includes('perda')) {
                win = false;
            } else {
                // Não conseguiu determinar — ignora
                if (DEBUG) console.log('[GarraBot] ⚠️ Resultado ambíguo, ignorando.', container.textContent.substring(0, 80));
                return;
            }
        }

        // Extrai valor monetário do resultado
        const textoValor = _lerTextoSeletor(SELETORES_RESULTADO.valor, container);
        let lucro = textoValor ? _parseValorMonetario(textoValor) : null;
        if (lucro === null) {
            // Fallback: se WIN estima +stake, se LOSS estima -stake
            lucro = win ? _ultimaStake : -_ultimaStake;
        }
        if (!win && lucro > 0) lucro = -lucro;  // garante sinal correto

        // Lê saldo atual pós-trade
        const saldoAtual = _lerSaldo();

        // Ativo e direção (opcionais — podem não aparecer no container)
        const textoAtivo   = _lerTextoSeletor(SELETORES_RESULTADO.ativo,    container) || 'Desconhecido';
        const textoDirecao = _lerTextoSeletor(SELETORES_RESULTADO.direcao,  container) || '—';

        // Hash para evitar envio duplicado do mesmo resultado
        const hash = `${win}_${lucro}_${saldoAtual}_${textoAtivo}_${Date.now().toString().slice(0, -3)}`;
        if (hash === _ultimoResultadoHash) {
            if (DEBUG) console.log('[GarraBot] 🔁 Resultado duplicado, ignorando.');
            return;
        }
        _ultimoResultadoHash = hash;

        // Atualiza contadores locais
        if (win) {
            _wins++;
            _profitTotal += lucro;
        } else {
            _losses++;
            _profitTotal += lucro; // lucro negativo
        }

        // Calcula próxima stake (Martingale simples se LOSS, volta ao valor base se WIN)
        let proxStake;
        if (!win) {
            proxStake = parseFloat((_ultimaStake * 2).toFixed(2));
        } else {
            proxStake = _ultimaStake; // mantém a stake base
        }

        // Detecta modo (demo/real)
        _modo = _detectarModo();

        const payload = {
            win:          win,
            lucro:        lucro,
            saldo:        saldoAtual,
            ativo:        textoAtivo,
            direcao:      textoDirecao,
            modo:         _modo,
            estrategia:   _estrategia,
            wins:         _wins,
            losses:       _losses,
            profit_total: _profitTotal,
            prox_stake:   proxStake,
        };

        console.log(`[GarraBot] 📊 Resultado detectado: ${win ? '✅ WIN' : '❌ LOSS'} | Lucro: ${lucro >= 0 ? '+' : ''}$${lucro.toFixed(2)} | Saldo: $${saldoAtual !== null ? saldoAtual.toFixed(2) : '?'}`);

        _enviarResultado(payload);
    }

    /**
     * Envia o payload de resultado para o servidor GarraBot.
     */
    function _enviarResultado(payload) {
        const corpo = JSON.stringify(payload);
        console.log(`[GarraBot] 📊 Enviando resultado para ${_servidorAtivo}/quotex/notificar-resultado:`, payload);
        _postComFallback('/quotex/notificar-resultado', corpo, (resp) => {
            if (resp && resp.ok) {
                console.log('[GarraBot] ✅ Telegram notificado com sucesso!');
            } else {
                console.warn('[GarraBot] ⚠️ Servidor rejeitou resultado:', resp);
            }
        });
    }

    // ════════════════════════════════════════════
    // OBSERVER DE RESULTADO (MutationObserver)
    //
    // Observa o DOM por containers de resultado que
    // aparecem após o fechamento de cada trade.
    // ════════════════════════════════════════════

    let _observadorResultado     = null;
    let _debounceResultadoTimer  = null;
    let _ultimoContainerVisto    = null;

    function _verificarResultadoDOM() {
        const container = _encontrarContainerResultado();
        if (!container) return;
        if (container === _ultimoContainerVisto) return;

        _ultimoContainerVisto = container;
        _processarResultado(container);

        // Atualiza saldo após resultado
        setTimeout(_verificarESincronizar, 800);
    }

    function _verificarResultadoComDebounce() {
        clearTimeout(_debounceResultadoTimer);
        _debounceResultadoTimer = setTimeout(_verificarResultadoDOM, 600);
    }

    function _iniciarObservadorResultado() {
        if (_observadorResultado) return;
        _observadorResultado = new MutationObserver(_verificarResultadoComDebounce);
        _observadorResultado.observe(document.body, {
            childList:     true,
            subtree:       true,
            attributes:    true,
            attributeFilter: ['class', 'style'],
        });
        if (DEBUG) console.log('[GarraBot] 👁️  MutationObserver de resultado ativo.');
    }

    // ════════════════════════════════════════════
    // OBSERVERS — SALDO
    // ════════════════════════════════════════════

    function _iniciarObservador() {
        if (_observador) return;
        _observador = new MutationObserver(_verificarComDebounce);
        _observador.observe(document.body, { childList: true, subtree: true, characterData: true });
        if (DEBUG) console.log('[GarraBot] 👁️  MutationObserver de saldo ativo.');
    }

    function _iniciarPolling() {
        if (_intervaloId) return;
        _intervaloId = setInterval(_verificarESincronizar, INTERVALO_MS);
        if (DEBUG) console.log(`[GarraBot] ⏱️  Polling a cada ${INTERVALO_MS}ms ativo.`);
    }

    // ════════════════════════════════════════════
    // API PÚBLICA — window.garrabot
    // ════════════════════════════════════════════
    window.garrabot = {
        /** Agenda entrada CALL/PUT na virada do minuto */
        entrarNaVirada,

        /** Agenda qualquer callback N ms antes da virada */
        esperarViradaMinuto,

        /** Cancela uma entrada agendada */
        cancelarVirada,

        /** Milissegundos até a próxima virada */
        msAteVirada: _msAteVirada,

        /** Força sincronização do saldo agora */
        sincronizarSaldo: _verificarESincronizar,

        /** Último saldo sincronizado */
        get ultimoSaldo() { return _ultimoSaldo; },

        /** true se há entrada agendada */
        get aguardandoVirada() { return _aguardandoVirada; },

        /** Wins acumulados nesta sessão */
        get wins() { return _wins; },

        /** Losses acumulados nesta sessão */
        get losses() { return _losses; },

        /** Profit total acumulado nesta sessão */
        get profitTotal() { return _profitTotal; },

        /**
         * Define o nome da estratégia que aparecerá na notificação Telegram.
         * Ex: window.garrabot.setEstrategia('Garra M1');
         */
        setEstrategia(nome) { _estrategia = nome || 'Quotex'; },

        /**
         * Define a stake base (usada como fallback quando o valor não é detectado no DOM).
         * Ex: window.garrabot.setStake(10);
         */
        setStake(valor) { _ultimaStake = parseFloat(valor) || 5.0; },

        /**
         * Força o processamento manual de um resultado (para testes).
         * Ex: window.garrabot.testarResultado(true, 12.50);
         */
        testarResultado(win, lucro) {
            _enviarResultado({
                win:          !!win,
                lucro:        parseFloat(lucro) || 0,
                saldo:        _ultimoSaldo,
                ativo:        'TESTE',
                direcao:      win ? 'CALL' : 'PUT',
                modo:         _modo,
                estrategia:   _estrategia,
                wins:         _wins,
                losses:       _losses,
                profit_total: _profitTotal,
                prox_stake:   _ultimaStake,
            });
        },

        /**
         * Diagnóstico: lista no console todos os elementos "balance" do DOM.
         * Use quando o saldo não atualiza: window.garrabot.diagnosticarSaldo()
         */
        diagnosticarSaldo: _diagnosticarSaldo,

        /** URL do servidor GarraBot configurado */
        serverUrl: GARRABOT_URL,

        /** Antecipação em ms configurada (padrão: 3000ms) */
        antecipacaoMs: ANTECIPACAO_MS,
    };

    // Compatibilidade com versões anteriores
    window.qxSincronizarSaldo = _verificarESincronizar;

    // ════════════════════════════════════════════
    // INICIALIZAÇÃO
    // ════════════════════════════════════════════
    function _init() {
        console.log('[GarraBot] 🚀 quotex_sync_banca.js v1.5 carregado.');
        console.log(`[GarraBot] 🎯 Servidor principal: ${GARRABOT_URL}`);
        console.log(`[GarraBot] 🔄 Fallback: ${GARRABOT_URL_FALLBACK}`);
        console.log('[GarraBot] ⏱️  Antecipação de entrada:', ANTECIPACAO_MS / 1000 + 's antes da virada do minuto');
        console.log('[GarraBot] 📊 Detecção automática de resultados: ATIVA');
        console.log('[GarraBot] 💡 Para agendar uma entrada: window.garrabot.entrarNaVirada("call")');
        console.log('[GarraBot] 🔬 Para testar notificação: window.garrabot.testarResultado(true, 10.0)');
        console.log('[GarraBot] 🔍 Para diagnosticar saldo: window.garrabot.diagnosticarSaldo()');

        // Inicia observer de resultado imediatamente
        _iniciarObservadorResultado();

        const elSaldo = _encontrarElementoSaldo();
        if (elSaldo) {
            console.log(`[GarraBot] ✅ Saldo encontrado: "${elSaldo.textContent.trim()}" (${elSaldo.className})`);
            _verificarESincronizar();
            _iniciarObservador();
            _iniciarPolling();
        } else {
            // Elemento não encontrado — roda o diagnóstico para ajudar a depurar
            console.warn('[GarraBot] ⚠️  Elemento de saldo não encontrado na inicialização.');
            console.warn('[GarraBot] 🔍 Rodando diagnóstico automático de saldo...');
            setTimeout(_diagnosticarSaldo, 2000);

            let tentativas = 0;
            const MAX_TENTATIVAS = 60;
            const espera = setInterval(() => {
                tentativas++;
                const el = _encontrarElementoSaldo();
                if (el) {
                    clearInterval(espera);
                    console.log(`[GarraBot] ✅ Saldo encontrado após ${tentativas * 500}ms: "${el.textContent.trim()}" (${el.className})`);
                    _verificarESincronizar();
                    _iniciarObservador();
                    _iniciarPolling();
                } else if (tentativas >= MAX_TENTATIVAS) {
                    clearInterval(espera);
                    console.error('[GarraBot] ❌ Elemento de saldo não encontrado em 30s. Execute: window.garrabot.diagnosticarSaldo()');
                    _iniciarPolling();
                }
            }, 500);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _init);
    } else {
        _init();
    }

})();
