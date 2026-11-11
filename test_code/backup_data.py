"""
数据备份脚本
在迁移前备份重要数据
"""
import os
import sys
import json
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import fetch_all
from dotenv import load_dotenv

load_dotenv()


def backup_table(table_name: str, output_dir: str):
    """备份单个表的数据"""
    try:
        print(f"\n📦 备份表: {table_name}")
        
        # 查询所有数据
        sql = f"SELECT * FROM {table_name}"
        rows = fetch_all(sql)
        
        if not rows:
            print(f"   ⚠️  表为空，跳过")
            return
        
        # 转换为JSON格式
        data = []
        for row in rows:
            if isinstance(row, tuple):
                # 如果是元组，需要获取列名
                continue  # 简化处理
            else:
                # 字典格式
                # 转换datetime为字符串
                row_dict = {}
                for key, value in row.items():
                    if isinstance(value, datetime):
                        row_dict[key] = value.isoformat()
                    elif isinstance(value, bytes):
                        row_dict[key] = None  # 跳过二进制数据
                    else:
                        row_dict[key] = value
                data.append(row_dict)
        
        # 保存到文件
        output_file = os.path.join(output_dir, f"{table_name}_backup.json")
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        print(f"   ✅ 已保存 {len(data)} 条记录到: {output_file}")
        return len(data)
        
    except Exception as e:
        print(f"   ❌ 备份失败: {e}")
        return 0


def main():
    """主函数"""
    print("\n" + "💾" * 30)
    print("RAG系统数据备份")
    print("💾" * 30)
    
    # 创建备份目录
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(
        os.path.dirname(__file__),
        '..',
        'backups',
        f'backup_{timestamp}'
    )
    
    os.makedirs(backup_dir, exist_ok=True)
    print(f"\n📁 备份目录: {backup_dir}")
    
    # 要备份的表
    tables = [
        'users',
        'file_metadata',
        'file_content'
    ]
    
    total_records = 0
    
    for table in tables:
        count = backup_table(table, backup_dir)
        if count:
            total_records += count
    
    # 创建备份信息文件
    info = {
        'backup_time': timestamp,
        'total_records': total_records,
        'tables': tables
    }
    
    info_file = os.path.join(backup_dir, 'backup_info.json')
    with open(info_file, 'w', encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    
    print("\n" + "=" * 60)
    print(f"✅ 备份完成!")
    print(f"   总记录数: {total_records}")
    print(f"   备份位置: {backup_dir}")
    print("=" * 60)
    
    print("\n📚 下一步:")
    print("   1. 查看备份文件确认数据完整")
    print("   2. 执行数据库迁移: python test_code/migrate_db.py")


if __name__ == "__main__":
    main()
