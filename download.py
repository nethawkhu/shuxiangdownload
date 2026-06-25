import asyncio
import os
import shutil
import json
import httpx
import logging
from playwright.async_api import async_playwright
import pypdf
from pypdf import PdfWriter

# ================= 配置区 =================
# 如果想下载其他书籍，请修改这里的 BOOK_ID
# 电子书 URL 末尾的那串字符就是 BOOK_ID，例如 https://shuxiang.chineseall.cn/book/read/FjdEj 的 ID 是 FjdEj
BOOK_ID = "FjdEj"
# ==========================================

BASE_URL = f"https://shyyjsdx.w.chineseall.cn/book/read/{BOOK_ID}"
API_BASE = "https://shyyjsdx.cahd.chineseall.cn/book"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 屏蔽 pypdf 内部输出的非致命红字警告日志（如 Object 247 0 not defined 等）
logging.getLogger("pypdf").setLevel(logging.ERROR)

async def fetch_total_pages(client, token):
    """在 Python 后端用指定的 token 获取书籍总页数"""
    if not token:
        return None
    url = f"{API_BASE}/detail/{BOOK_ID}"
    headers = {
        "sx-token": token,
        "referer": "https://shyyjsdx.w.chineseall.cn/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        response = await client.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("success") and "result" in data:
                page_count = data["result"].get("page")
                if page_count:
                    return int(page_count)
    except Exception:
        pass
    return None

async def check_full_access(client, token):
    """在 Python 后端用指定的 token 探测是否已获得完整阅读权限"""
    if not token:
        return False
    url = f"{API_BASE}/bcsPageUrl/{BOOK_ID}/pdf/11"
    headers = {
        "sx-token": token,
        "referer": "https://shyyjsdx.w.chineseall.cn/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        response = await client.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("success") and "result" in data:
                if data["result"].get("resourceUrl"):
                    return True
    except Exception:
        pass
    return False

async def get_credentials():
    """通过 Playwright 注入补丁 JS 并捕获 Token、解密密钥和总页数"""
    captured_tokens = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        
        # 1. 拦截并替换 main.js
        async def route_main_js(route):
            patch_path = os.path.join(SCRIPT_DIR, "main_patched.js")
            with open(patch_path, "r", encoding="utf-8") as f:
                js_content = f.read()
            await route.fulfill(
                status=200,
                content_type="application/javascript",
                body=js_content
            )
            print("[核心] 已成功将解密钩子注入到网页核心逻辑中")
            
        await page.route("**/main.*.js", route_main_js)
        
        # 2. 监听网络请求捕获 Token
        async def handle_request(request):
            url = request.url
            if "bcsPageUrl" in url:
                token = request.headers.get("sx-token")
                if token:
                    captured_tokens.append(token)
                    
        page.on("request", handle_request)
        
        print("\n正在拉起浏览器并载入电子书页面...")
        try:
            await page.goto(BASE_URL, timeout=60000, wait_until="networkidle")
        except Exception as e:
            print(f"载入页面超时或出错 (将继续尝试获取权限): {e}")
        
        # 触发解密初始化
        try:
            await page.evaluate("""() => {
                fetch("https://shyyjsdx.cahd.chineseall.cn/book/conf/7cH1g/pdf");
            }""")
        except Exception:
            pass
            
        # 3. 轮询检测解密密钥、最新 Token 和阅读权限
        print("开始探测电子书权限与密钥...")
        book_read_key = None
        total_pages = None
        has_full_access = False
        
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=5)
        async with httpx.AsyncClient(limits=limits) as client:
            for i in range(120): # 最长等待 120 秒
                # 检测密钥
                if not book_read_key:
                    try:
                        val = await page.evaluate("window.BOOK_READ_KEY")
                        if val:
                            book_read_key = val
                            print(f"[成功] 已捕获解密密钥 (book_read_key): {book_read_key}")
                    except Exception:
                        pass
                
                # 获取图书总页数 (优先从 Python 后台获取，最稳定)
                if not total_pages and captured_tokens:
                    try:
                        pages = await fetch_total_pages(client, captured_tokens[-1])
                        if pages:
                            total_pages = pages
                            print(f"检测到书籍总页数: {total_pages} 页")
                    except Exception:
                        pass
                        
                # 备用：从页面 evaluate 获取图书总页数
                if not total_pages:
                    try:
                        detail_val = await page.evaluate(f'() => fetch("https://shyyjsdx.cahd.chineseall.cn/book/detail/{BOOK_ID}").then(r => r.json()).then(d => d.result)')
                        if detail_val:
                            total_pages = int(detail_val.get("page", 0))
                            print(f"检测到书籍总页数: {total_pages} 页")
                    except Exception:
                        pass
                
                # 检查捕获的最新 token 是否有完整权限
                if captured_tokens:
                    latest_token = captured_tokens[-1]
                    if await check_full_access(client, latest_token):
                        has_full_access = True
                        print("[权限提示] 检测到当前已具备完整版阅读与下载权限！")
                        break
                
                # 如果没有获得权限，且已经等待了 3 秒以上，每隔 3 秒模拟一次翻页按键以驱动网络请求和 Token 更新
                if not has_full_access and i > 2 and i % 3 == 0:
                    try:
                        print("[探测] 模拟翻页操作以同步并刷新登录 Token...")
                        await page.keyboard.press("ArrowRight")
                    except Exception:
                        pass
                
                # 判断退出条件：获取到密钥，并且已经拥有完整权限（或者确认书籍总共就不超过10页）
                if book_read_key and (has_full_access or (total_pages and total_pages <= 10)):
                    break
                    
                # 提示用户登录
                if i == 5 and not has_full_access:
                    print("\n" + "="*70)
                    print("[重要提示] 发现您当前未登录，或是在【脚本拉起的浏览器】中未登录。")
                    print("请在弹出的 Chromium 浏览器中，登录您的读者证/机构账号。")
                    print("登录成功后，脚本在翻页时会自动感应到最新状态并开始下载整本书！")
                    print("="*70 + "\n")
                    
                await asyncio.sleep(1)
                
        final_token = captured_tokens[-1] if captured_tokens else None
        await browser.close()
        return final_token, book_read_key, total_pages, has_full_access

async def fetch_page_url(client, token, page_num, semaphore):
    """请求 bcsPageUrl 接口获取单页 PDF 的下载链接"""
    url = f"{API_BASE}/bcsPageUrl/{BOOK_ID}/pdf/{page_num}"
    headers = {
        "sx-token": token,
        "referer": "https://shyyjsdx.w.chineseall.cn/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    async with semaphore:
        for attempt in range(3):
            try:
                response = await client.get(url, headers=headers, timeout=15)
                if response.status_code == 200:
                    data = response.json()
                    if data.get("success") and "result" in data:
                        resource_url = data["result"].get("resourceUrl")
                        if resource_url:
                            return page_num, resource_url
                await asyncio.sleep(1)
            except Exception as e:
                print(f"获取第 {page_num} 页 URL 出错 (尝试 {attempt+1}/3): {e}")
        return page_num, None

async def download_and_decrypt_file(client, page_num, url, book_read_key, temp_dir, semaphore):
    """下载单页加密的 PDF，使用密钥进行 PDF 内部解密，最终保存为干净无密码的单页 PDF"""
    if not url:
        return page_num, False
        
    dest_path = os.path.join(temp_dir, f"{page_num:04d}.pdf")
    
    async with semaphore:
        for attempt in range(3):
            try:
                response = await client.get(url, timeout=30)
                if response.status_code == 200:
                    enc_data = response.content
                    
                    # 写入临时加密文件
                    temp_enc_path = os.path.join(temp_dir, f"{page_num:04d}_enc.pdf")
                    with open(temp_enc_path, "wb") as f:
                        f.write(enc_data)
                        
                    # 用 pypdf 对加密的单页 PDF 进行密码解密
                    reader = pypdf.PdfReader(temp_enc_path)
                    if reader.is_encrypted:
                        reader.decrypt(book_read_key)
                        
                    # 重新将解密后的 PDF 页面写入（实现完全去密码限制）
                    writer = PdfWriter()
                    writer.append(reader)
                    with open(dest_path, "wb") as f_out:
                        writer.write(f_out)
                    writer.close()
                    
                    # 删除加密原文件
                    os.remove(temp_enc_path)
                    return page_num, True
                await asyncio.sleep(1)
            except Exception as e:
                print(f"下载/解密第 {page_num} 页失败 (尝试 {attempt+1}/3): {e}")
        return page_num, False

async def main():
    token, book_read_key, total_pages, has_full_access = await get_credentials()
    if not token or not book_read_key:
        print("\n[错误] 未成功捕获 Token 或解密密钥，下载已取消，请重新运行。")
        return
        
    temp_dir = os.path.join(SCRIPT_DIR, "temp_pdfs")
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    os.makedirs(temp_dir, exist_ok=True)
    
    # 控制并发
    api_semaphore = asyncio.Semaphore(5)
    download_semaphore = asyncio.Semaphore(10)
    
    limits = httpx.Limits(max_keepalive_connections=5, max_connections=20)
    async with httpx.AsyncClient(limits=limits) as client:
        # 兜底：如果总页数没拿到，在后台用获得的 Token 强行查询并同步一次
        if not total_pages:
            try:
                pages = await fetch_total_pages(client, token)
                if pages:
                    total_pages = pages
            except Exception:
                pass
                
        # 1. 获取所有页面的下载直链
        print("\n开始获取每页 PDF 的加密下载链接...")
        page_urls = {}
        
        download_limit = total_pages if has_full_access else min(total_pages or 10, 10)
        
        # 优化提示语言，避免总页数解析不出时输出 None
        if not has_full_access:
            limit_str = f"前 {download_limit} 页" if download_limit else "前 10 页试读版"
            print(f"[警告] 当前仅具备试读权限，将为您下载并生成 {limit_str} PDF。")
        else:
            limit_str = f"整本书，共 {download_limit} 页" if download_limit else "整本书（已启用动态页数探测）"
            print(f"[提示] 已拥有完整权限，将为您下载并生成 {limit_str}。")
            
        if download_limit:
            tasks = [fetch_page_url(client, token, i, api_semaphore) for i in range(1, download_limit + 1)]
            results = await asyncio.gather(*tasks)
            for page_num, r_url in results:
                if r_url:
                    page_urls[page_num] = r_url
        else:
            # 备用动态探测
            current_page = 1
            consecutive_failures = 0
            while consecutive_failures < 5:
                tasks = [fetch_page_url(client, token, p, api_semaphore) for p in range(current_page, current_page + 20)]
                results = await asyncio.gather(*tasks)
                results.sort(key=lambda x: x[0])
                
                any_success = False
                for page_num, r_url in results:
                    if r_url:
                        page_urls[page_num] = r_url
                        any_success = True
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                        
                if not any_success:
                    break
                current_page += 20
                print(f"已获取前 {current_page - 1} 页 of 链接...")
                await asyncio.sleep(0.5)
                
        total_fetched = len(page_urls)
        print(f"链接获取完成，成功拿到 {total_fetched} 个页面的加密 PDF 直链。")
        
        # 2. 多线程下载并动态解密
        print("\n开始下载并解密所有 PDF 单页...")
        download_tasks = [
            download_and_decrypt_file(client, page_num, url, book_read_key, temp_dir, download_semaphore)
            for page_num, url in page_urls.items()
        ]
        
        download_results = await asyncio.gather(*download_tasks)
        success_count = sum(1 for page_num, success in download_results if success)
        print(f"下载与解密完成，成功页数: {success_count}/{total_fetched}")
        
    # 3. 合并 PDF
    if success_count > 0:
        print("\n正在按页码顺序合并所有 PDF 页面...")
        merger = PdfWriter()
        
        sorted_pages = sorted([int(f.split('.')[0]) for f in os.listdir(temp_dir) if f.endswith('.pdf')])
        
        for page_num in sorted_pages:
            file_path = os.path.join(temp_dir, f"{page_num:04d}.pdf")
            try:
                merger.append(file_path)
            except Exception as e:
                print(f"合并第 {page_num} 页失败: {e}")
                
        output_path = os.path.join(SCRIPT_DIR, f"book_{BOOK_ID}.pdf")
        with open(output_path, "wb") as f_out:
            merger.write(f_out)
        merger.close()
        
        print(f"\n[恭喜] 电子书下载并合并成功！")
        print(f"文件保存路径: {output_path}")
    else:
        print("未成功下载解密任何页面，合并已取消。")
        
    # 清理临时文件
    try:
        shutil.rmtree(temp_dir)
        print("临时缓存文件已清理干净。")
    except Exception as e:
        pass

if __name__ == "__main__":
    asyncio.run(main())
