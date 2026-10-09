def _get_base_url():
    return 'https://bot-garra-uv2r.onrender.com'

with open('main.py', 'r', encoding='utf-8') as f:
    src = f.read()

start_marker = 'def rota_pocket_login_page():'
start = src.find(start_marker)
end_marker = '\n@app.route'
end = src.find(end_marker, start + 100)
func_src = src[start:end]

ns = {'_get_base_url': _get_base_url}
try:
    exec(func_src, ns)
    result = ns['rota_pocket_login_page']()
    print(f'OK! Funcao executou, resultado tem {len(result)} chars')
    checks = [
        ('WebSocket.prototype.send', 'monkey-patch WebSocket'),
        ('isDemo', 'isDemo no bookmarklet'),
        ('javascript:(function', 'bookmarklet javascript:'),
        ('"session":', 'chave session no SSID'),
    ]
    for pattern, label in checks:
        if pattern in result:
            print(f'OK! {label}')
        else:
            print(f'FALHA! {label} NAO encontrado')
except Exception as e:
    import traceback
    print(f'ERRO: {type(e).__name__}: {str(e)[:400]}')
    traceback.print_exc()
