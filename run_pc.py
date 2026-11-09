# -*- coding: utf-8 -*-
"""내 PC를 폰 앱 서버로 쓰는 실행 파일. 같은 와이파이의 폰에서 http://PC주소:7860 으로 접속한다."""
import json, os, socket, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '폰앱_데이터')
os.makedirs(DATA, exist_ok=True)
CFG = os.path.join(DATA, 'pc_설정.json')


def need(mod, pipname=None):
    try:
        __import__(mod)
    except ImportError:
        print(f'필요한 부품({pipname or mod})을 설치합니다. 1~2분 걸릴 수 있습니다…')
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--quiet', pipname or mod])


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1)); ip = s.getsockname()[0]; s.close()
        return ip
    except OSError:
        return '내PC주소'


def main():
    for m, p in (('flask', 'flask'), ('openpyxl', 'openpyxl'), ('yfinance', 'yfinance')):
        need(m, p)
    cfg = {}
    try:
        cfg = json.load(open(CFG, encoding='utf-8'))
    except (OSError, ValueError):
        pass
    if not cfg.get('password'):
        print('\n처음 한 번만: 폰에서 앱에 들어갈 때 쓸 비밀번호를 정하세요.')
        while len(cfg.get('password', '')) < 4:
            cfg['password'] = input('앱 비밀번호 (4자 이상): ').strip()
        json.dump(cfg, open(CFG, 'w', encoding='utf-8'), ensure_ascii=False)
    os.environ['APP_PASSWORD'] = cfg['password']
    os.environ['DATA_DIR'] = DATA
    port = int(os.environ.get('PORT', 7860))
    sys.path.insert(0, HERE)
    import server
    ip = lan_ip()
    print('\n' + '=' * 60)
    print(' 적정주가 폰 앱 서버가 켜졌습니다 (이 창을 닫으면 꺼집니다)')
    print('=' * 60)
    print(f'\n 폰(같은 와이파이)에서 브라우저로 여세요:\n\n     http://{ip}:{port}\n')
    print(' 처음 한 번: 폰에서 비밀번호 입력 → [설정]에서 이메일(SEC)·DART 키 입력')
    print(' Windows 방화벽 창이 뜨면 [액세스 허용]을 누르세요.')
    print(' 집 밖에서 쓰려면 배포안내.txt 의 "Tailscale" 방법을 보세요.\n')
    try:
        need('qrcode')
        import qrcode
        q = qrcode.QRCode(border=1); q.add_data(f'http://{ip}:{port}'); q.make()
        q.print_ascii(invert=True)
    except Exception:  # noqa: BLE001
        pass
    server.app.run(host='0.0.0.0', port=port, threaded=True)


if __name__ == '__main__':
    main()
