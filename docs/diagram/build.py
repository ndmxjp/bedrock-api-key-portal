"""README 用アーキテクチャ図を公式 AWS アイコンで描く（Graphviz neato、座標は手置き）。

    python3 -m venv .venv && .venv/bin/pip install diagrams   # アイコン取得のため
    brew install graphviz
    .venv/bin/python docs/diagram/build.py                    # docs/images/architecture.png を更新
"""
import subprocess, pathlib, tempfile
OUT = pathlib.Path(__file__).resolve().parents[1] / "images"
import diagrams
R = pathlib.Path(diagrams.__file__).parents[1] / "resources/aws"  # diagrams パッケージ同梱の公式アイコン
I = {
  "users": R/"general/users.png", "cf": R/"network/cloudfront.png", "s3": R/"storage/simple-storage-service-s3.png",
  "cognito": R/"security/cognito.png", "apigw": R/"network/api-gateway.png", "lambda": R/"compute/lambda.png",
  "ddb": R/"database/dynamodb.png", "iam": R/"security/identity-and-access-management-iam.png",
  "key": R/"security/identity-and-access-management-iam-long-term-security-credential.png",
  "policy": R/"security/identity-and-access-management-iam-permissions.png", "ce": R/"cost/cost-explorer.png",
  "cw": R/"management/cloudwatch.png", "budgets": R/"cost/budgets.png", "mp": R/"general/marketplace.png",
  "bedrock": R/"ml/bedrock.png",
}
for p in I.values(): assert p.exists(), p
FONT = "Hiragino Sans"
PT = 72

def node(id, icon, label, x, y, size=58):
    lab = label.replace("\n", "<BR/>")
    return (f'{id} [layer="fg", shape=none, margin=0, label=<<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="1">'
            f'<TR><TD FIXEDSIZE="TRUE" WIDTH="{size}" HEIGHT="{size}"><IMG SRC="{I[icon]}"/></TD></TR>'
            f'<TR><TD><FONT POINT-SIZE="9.5">{lab}</FONT></TD></TR></TABLE>>, pos="{x*PT},{y*PT}!"];')

def box(id, label, x0, y0, x1, y1, fill="#fafbfd", color="#c3c8d3"):
    w, h = x1 - x0, y1 - y0
    return (f'{id} [layer="bg", shape=box, style="rounded,filled", fillcolor="{fill}", color="{color}", penwidth=1, '
            f'label=<<FONT POINT-SIZE="10.5" COLOR="#5c6373">{label}</FONT>>, labelloc=t, '
            f'width={w}, height={h}, fixedsize=true, pos="{(x0+w/2)*PT},{(y0+h/2)*PT}!"];')

N, E = [], []
_pid = [0]
def edge(a, b, label="", **kw):
    attrs = {"layer": "fg", "fontsize": "9", "color": "#5c6373", "fontcolor": "#374151", "arrowsize": "0.7"}
    attrs.update(kw)
    if label: attrs["label"] = label
    attr_str = ", ".join(f'{k}="{v}"' for k, v in attrs.items())
    E.append(f"{a} -> {b} [{attr_str}];")

def text(label, x, y, color="#374151"):
    _pid[0] += 1
    lab = label.replace("\n", '<BR ALIGN="LEFT"/>')
    N.append(f'tx{_pid[0]} [layer="fg", shape=plaintext, margin=0, fontsize=9, fontcolor="{color}", '
             f'label=<{lab}<BR ALIGN="LEFT"/>>, pos="{x*PT},{y*PT}!"];')

def route(a, pts, b, label="", label_seg=-1, **kw):
    """Edge a->b bent through invisible waypoints (inches)."""
    ids = []
    for (x, y) in pts:
        _pid[0] += 1
        pid = f"wp{_pid[0]}"
        N.append(f'{pid} [layer="fg", shape=point, width=0, height=0, label="", pos="{x*PT},{y*PT}!"];')
        ids.append(pid)
    chain = [a] + ids + [b]
    segs = list(zip(chain, chain[1:]))
    if label_seg < 0: label_seg += len(segs)
    for i, (u, v) in enumerate(segs):
        k = dict(kw)
        if i < len(segs) - 1: k["arrowhead"] = "none"
        if i > 0: k.pop("tailport", None)
        if i < len(segs) - 1: k.pop("headport", None)
        edge(u, v, label if i == label_seg else "", **k)

# ---- layout (inches; y up). Row A (top) y=6.0, Row B y=3.9, Row C y=1.4
N.append(box("acct", "AWS アカウント（ポータル）", 1.8, 0.1, 14.2, 7.5, fill="#f7f8fb", color="#9aa1b1"))
N.append(box("c_edge", "配信・認証", 2.0, 3.0, 6.1, 7.1))
N.append(box("c_api",  "API", 6.4, 3.0, 9.6, 7.1))
N.append(box("c_ops",  "運用", 9.9, 3.0, 14.0, 7.1))
N.append(box("c_iam",  "利用者ごとの IAM", 6.4, 0.3, 11.2, 2.7))
N.append(box("c_dep",  "デプロイ時", 2.0, 0.3, 6.1, 2.7))

N.append(node("users","users","PoC 参加者 / 管理者\n許可ドメインのメール", 0.6, 6.0, 64))
N.append(node("cf","cf","CloudFront\n同一オリジン / CSP\n任意で IP 制限", 3.0, 6.0))
N.append(node("cognito","cognito","Cognito User Pool\nパスワード認証\n許可ドメインのみ", 5.0, 6.0))
N.append(node("s3","s3","S3\nSPA + フォント", 3.0, 3.9))
N.append(node("apigw","apigw","HTTP API\nJWT authorizer", 7.1, 6.0))
N.append(node("lambda","lambda","Lambda (Python)\n発行 / 失効 / 管理", 8.9, 6.0))
N.append(node("ddb","ddb","DynamoDB\n状態・枠・設定・キャッシュ", 7.1, 3.9))
N.append(node("ce","ce","Cost Explorer\n利用者別の利用料", 10.7, 6.0))
N.append(node("cw","cw","CloudWatch\nアラーム", 12.0, 6.0))
N.append(node("budgets","budgets","Budgets\n月額アラート", 13.3, 6.0))
N.append(node("iam","iam","IAM ユーザー\n/bedrock-poc/…", 7.3, 1.4))
N.append(node("key","key","Bedrock API キー\n(service-specific credential)", 8.9, 1.4))
N.append(node("policy","policy","顧客管理ポリシー\n= Permissions Boundary", 10.5, 1.4))
N.append(node("agree","lambda","デプロイ時 Lambda\nモデル利用規約へ自動同意", 3.0, 1.4))
N.append(node("mp","mp","AWS Marketplace\nモデル利用規約", 5.0, 1.4))
N.append(node("bedrock","bedrock","Amazon Bedrock\n許可した提供元のモデル", 15.5, 1.4, 64))

edge("users","cf","HTTPS")
route("users",[(0.6,5.05),(5.0,5.05)],"cognito","サインイン", label_seg=1, style="dashed", tailport="s", headport="s")
edge("cf","s3","/*")
route("cf",[(3.0,6.85),(7.1,6.85)],"apigw","/api/* + X-Origin-Verify", label_seg=1, tailport="n", headport="n")
edge("cognito","apigw","", style="dashed"); text("ID トークン", 6.05, 6.18)
edge("apigw","lambda","claims")
edge("lambda","ddb","", dir="both", tailport="w", headport="n")
edge("lambda","key","", tailport="s", headport="n"); text("CreateUser\nCreateServiceSpecificCredential\nAttachUserPolicy", 9.0, 4.9)
edge("key","iam","", style="dotted", arrowhead="none")
route("iam",[(7.3,0.55),(10.5,0.55)],"policy","attach + boundary", label_seg=1, style="dotted", tailport="s", headport="s")
edge("lambda","ce","", style="dashed"); text("GetCostAndUsage", 9.72, 6.2)
route("lambda",[(8.9,6.85),(12.0,6.85)],"cw","アラーム", label_seg=1, style="dashed", tailport="n", headport="n")
edge("agree","mp","", style="dashed"); text("CreateFoundationModelAgreement", 4.0, 2.15)
route("mp",[(5.0,-0.2),(15.5,-0.2)],"bedrock","規約同意", label_seg=1, style="dotted", tailport="s", headport="s")
edge("policy","bedrock","推論のみ許可", style="dotted")
route("users",[(0.6,-0.6),(15.5,-0.6)],"bedrock","発行された API キーで直接呼び出し（Bearer トークン）", label_seg=1,
      color="#5a4bd1", fontcolor="#5a4bd1", penwidth="2.4", tailport="s", headport="s")

dot = f'''digraph G {{
  graph [bgcolor="white", pad="0.4", dpi="160", splines="spline", overlap="true", fontname="{FONT}", layers="bg:fg"];
  node [fontname="{FONT}"];
  edge [fontname="{FONT}"];
  {chr(10).join(N)}
  {chr(10).join(E)}
}}'''
with tempfile.NamedTemporaryFile("w", suffix=".dot", delete=False) as f:
    f.write(dot)
subprocess.run(["neato", "-n2", "-Tpng", f.name, "-o", str(OUT / "architecture.png")], check=True)
print("rendered")
