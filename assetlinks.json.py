@app.route('/.well-known/assetlinks.json')
def assetlinks_json():
    from flask import Response
    try:
        with open('assetlinks.json', 'r', encoding='utf-8') as f:
            data = f.read()
        resp = Response(data, mimetype='application/json')
        resp.headers['Cache-Control'] = 'public, max-age=86400'
        return resp
    except FileNotFoundError:
        return jsonify({"erro": "assetlinks.json não encontrado"}), 404