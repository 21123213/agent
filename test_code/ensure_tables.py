"""
确保 document_elements / document_chunks 等表存在（非交互式）
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

from app.database import execute, query

STATEMENTS = [
    """
    ALTER TABLE file_metadata
    ADD COLUMN total_elements INT DEFAULT 0 COMMENT '文档元素总数'
    """,
    """
    ALTER TABLE file_metadata
    ADD COLUMN total_chunks INT DEFAULT 0 COMMENT '切片总数'
    """,
    """
    ALTER TABLE file_metadata
    ADD COLUMN embedded_chunks INT DEFAULT 0 COMMENT '已向量化的切片数'
    """,
    """
    ALTER TABLE file_metadata
    ADD COLUMN elements_stats JSON COMMENT '元素类型统计'
    """,
    """
    CREATE TABLE IF NOT EXISTS document_elements (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        file_id BIGINT NOT NULL COMMENT '文件ID',
        element_index INT NOT NULL COMMENT '元素序号',
        element_type VARCHAR(50) NOT NULL COMMENT '元素类型',
        text_content TEXT COMMENT '文本内容',
        metadata JSON COMMENT '元数据',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_file_id (file_id),
        INDEX idx_element_type (element_type),
        UNIQUE KEY uk_file_element (file_id, element_index),
        FOREIGN KEY (file_id) REFERENCES file_metadata(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    ALTER TABLE document_chunks
    ADD COLUMN content_types JSON COMMENT '内容类型 text/table/image'
    """,
    """
    ALTER TABLE document_chunks
    ADD COLUMN element_types JSON COMMENT 'Unstructured 元素类型列表'
    """,
    """
    CREATE TABLE IF NOT EXISTS document_chunks (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        file_id BIGINT NOT NULL COMMENT '文件ID',
        chunk_index INT NOT NULL COMMENT '切片序号',
        chunk_text LONGTEXT NOT NULL COMMENT '切片内容',
        char_count INT NOT NULL COMMENT '字符数',
        element_count INT NOT NULL COMMENT '包含的元素数量',
        element_ids JSON COMMENT '包含的元素ID列表',
        page_numbers JSON COMMENT '涉及的页码',
        content_types JSON COMMENT '内容类型 text/table/image',
        element_types JSON COMMENT 'Unstructured 元素类型列表',
        vector_id VARCHAR(100) COMMENT 'Milvus向量ID',
        is_embedded TINYINT(1) DEFAULT 0 COMMENT '是否已向量化',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        embedded_at TIMESTAMP NULL DEFAULT NULL COMMENT '向量化时间',
        INDEX idx_file_id (file_id),
        INDEX idx_vector_id (vector_id),
        INDEX idx_is_embedded (is_embedded),
        UNIQUE KEY uk_file_chunk (file_id, chunk_index),
        FOREIGN KEY (file_id) REFERENCES file_metadata(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]


def run():
    for sql in STATEMENTS:
        try:
            execute(sql.strip())
            print("OK:", sql.strip().split('\n')[0][:80])
        except Exception as e:
            err = str(e).lower()
            if 'duplicate column' in err or 'already exists' in err:
                print("SKIP (exists):", sql.strip().split('\n')[1].strip()[:60])
            else:
                print("FAIL:", e)
                raise

    tables = [list(r.values())[0] for r in query('SHOW TABLES')]
    print("\nTables:", tables)
    for t in ('document_elements', 'document_chunks'):
        print(f"  {t}:", "OK" if t in tables else "MISSING")


if __name__ == '__main__':
    run()
