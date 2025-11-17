"""
本地下载 NLTK 数据到项目目录

在开发机上运行一次，将 NLTK 数据下载到项目的 nltk_data/ 目录
然后提交到 git，Docker 构建时直接 COPY，避免每次构建都从网络下载

使用方式:
    python scripts/download_nltk_data.py
"""

import ssl
import sys
from pathlib import Path

def download_nltk_to_project():
    """下载 NLTK 数据到项目目录"""
    try:
        import nltk
    except ImportError:
        print("❌ 错误: 未安装 nltk 库")
        print("请先安装: pip install nltk")
        sys.exit(1)
    
    # 项目根目录
    project_root = Path(__file__).parent.parent
    nltk_data_dir = project_root / "nltk_data"
    nltk_data_dir.mkdir(exist_ok=True)
    
    print("="*60)
    print("SAG - NLTK 数据本地下载工具")
    print("="*60)
    print(f"\n📁 下载目录: {nltk_data_dir}")
    
    # 处理 SSL 证书问题
    try:
        _create_unverified_https_context = ssl._create_unverified_context
    except AttributeError:
        pass
    else:
        ssl._create_default_https_context = _create_unverified_https_context
    
    # 需要的资源列表
    resources = ['punkt', 'punkt_tab']
    
    print("\n开始下载 NLTK 数据...")
    success_count = 0
    
    for resource in resources:
        print(f"\n📥 下载 {resource}...")
        try:
            nltk.download(resource, download_dir=str(nltk_data_dir), quiet=False)
            print(f"✓ {resource} 下载完成")
            success_count += 1
        except Exception as e:
            print(f"✗ {resource} 下载失败: {e}")
    
    # 验证
    print("\n" + "="*60)
    print("验证下载的数据...")
    print("="*60)
    
    # 临时添加到 NLTK 的搜索路径
    if str(nltk_data_dir) not in nltk.data.path:
        nltk.data.path.insert(0, str(nltk_data_dir))
    
    all_ok = True
    for resource in resources:
        resource_path = {
            'punkt': 'tokenizers/punkt',
            'punkt_tab': 'tokenizers/punkt_tab'
        }.get(resource, resource)
        
        try:
            path = nltk.data.find(resource_path)
            print(f"✓ {resource}: {path}")
        except Exception as e:
            print(f"✗ {resource} 验证失败: {e}")
            all_ok = False
    
    # 总结
    print("\n" + "="*60)
    if all_ok and success_count == len(resources):
        print("✅ 所有 NLTK 数据已成功下载到项目目录！")
        print(f"\n📁 位置: {nltk_data_dir}")
        print(f"📦 已下载: {', '.join(resources)}")
        
        # 检查目录大小
        total_size = sum(f.stat().st_size for f in nltk_data_dir.rglob('*') if f.is_file())
        size_mb = total_size / (1024 * 1024)
        print(f"💾 总大小: {size_mb:.2f} MB")
        
        print("\n下一步:")
        print("  1. 将数据提交到 git:")
        print("     git add nltk_data/")
        print("     git commit -m 'Add pre-downloaded NLTK data'")
        print("     git push")
        print("\n  2. 重新构建 Docker 镜像:")
        print("     docker-compose build api")
    else:
        print("⚠️  部分资源下载失败，请检查网络连接后重试")
        sys.exit(1)
    
    print("="*60)

if __name__ == "__main__":
    download_nltk_to_project()

