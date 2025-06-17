"""
Enterprise Supply Chain Analytics Platform
A comprehensive data analytics application with:
- Direct SQL Server database integration
- AI-powered natural language queries with Claude
- Supply chain specific business intelligence
- Multi-user enterprise features
- Advanced data visualizations
- Query history and performance optimization
"""

import os
import json
import pandas as pd
import pyodbc
from flask import Flask, render_template_string, request, jsonify, session
import plotly.express as px
import plotly.graph_objects as go
from plotly.utils import PlotlyJSONEncoder
from datetime import datetime, timedelta
import logging
from typing import Dict, List, Optional, Tuple, Any
import re
from dotenv import load_dotenv
from sqlalchemy import create_engine, text, MetaData, inspect
from sqlalchemy.exc import SQLAlchemyError
import hashlib
from cachetools import TTLCache
import threading


# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SupplyChainAnalytics:
    def __init__(self):
        self.app = Flask(__name__)
        self.setup_config()
        self.setup_database()
        self.setup_cache()
        self.setup_routes()
        
        # Supply chain business context for Claude
        self.business_context = {
            'domain': 'supply_chain_management',
            'common_metrics': [
                'inventory_turnover', 'fill_rate', 'lead_time', 'on_time_delivery',
                'supplier_performance', 'cost_analysis', 'demand_forecasting',
                'stock_levels', 'order_cycle_time', 'quality_metrics'
            ],
            'typical_tables': [
                'suppliers', 'products', 'inventory', 'purchase_orders', 
                'sales_orders', 'shipments', 'warehouses', 'customers',
                'stock_movements', 'quality_control', 'costs', 'deliveries'
            ]
        }
        
    def setup_config(self):
        """Initialize configuration with environment variables"""
        # Validate required environment variables
        required_vars = ['ANTHROPIC_API_KEY', 'SECRET_KEY', 'SSMS_CONN_STR']
        missing_vars = [var for var in required_vars if not os.environ.get(var)]
        
        if missing_vars:
            logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
            
        # Anthropic Configuration
        self.anthropic_client = None
        self.model_name = os.environ.get('ANTHROPIC_MODEL', 'claude-3-sonnet-20240229')
        
        try:
            import anthropic
            self.anthropic_client = anthropic.Anthropic(
                api_key=os.environ.get('ANTHROPIC_API_KEY'),
                base_url=os.environ.get('ANTHROPIC_API_BASE', 'https://api.anthropic.com')
            )
            logger.info("Anthropic Claude client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Anthropic client: {e}")
            self.anthropic_client = None
            
        # Flask configuration
        self.app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
        self.app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))
        
        # Database configuration
        self.conn_str = os.environ.get('SSMS_CONN_STR')
        self.database_name = os.environ.get('DATABASE_NAME', 'Supplychain')
        self.max_query_time = int(os.environ.get('MAX_QUERY_TIME', 60))
        self.max_result_rows = int(os.environ.get('MAX_RESULT_ROWS', 10000))
        
        # Business configuration
        self.business_domain = os.environ.get('BUSINESS_DOMAIN', 'supply_chain_management')
        self.company_name = os.environ.get('COMPANY_NAME', 'Your Company')
        
        # Create necessary directories
        for folder in ['cache', 'exports', 'logs']:
            os.makedirs(folder, exist_ok=True)
            
        # Logging configuration
        log_level = os.environ.get('LOG_LEVEL', 'INFO')
        logging.basicConfig(
            level=getattr(logging, log_level),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('logs/app.log'),
                logging.StreamHandler()
            ]
        )

    def setup_database(self):
        """Initialize database connection and engine"""
        try:
            # Create SQLAlchemy engine for better connection management
            # Convert pyodbc connection string to SQLAlchemy format
            sqlalchemy_url = f"mssql+pyodbc:///?odbc_connect={self.conn_str}"
            
            self.engine = create_engine(
                sqlalchemy_url,
                pool_size=int(os.environ.get('DB_POOL_SIZE', 5)),
                max_overflow=int(os.environ.get('DB_MAX_OVERFLOW', 10)),
                pool_timeout=int(os.environ.get('DB_TIMEOUT', 30)),
                echo=False  # Set to True for SQL debugging
            )
            
            # Test connection
            with self.engine.connect() as conn:
                result = conn.execute(text("SELECT 1 as test"))
                logger.info("Database connection established successfully")
                
            # Initialize metadata for schema discovery
            self.metadata = MetaData()
            self.inspector = inspect(self.engine)
            
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            self.engine = None
            self.metadata = None
            self.inspector = None

    def setup_cache(self):
        """Initialize caching system"""
        # Schema cache (longer TTL)
        self.schema_cache = TTLCache(
            maxsize=100, 
            ttl=int(os.environ.get('SCHEMA_CACHE_DURATION', 3600))  # 1 hour default
        )
        
        # Query result cache (shorter TTL)
        self.query_cache = TTLCache(
            maxsize=500,
            ttl=int(os.environ.get('QUERY_CACHE_DURATION', 900))  # 15 minutes default
        )
        
        # Thread lock for cache operations
        self.cache_lock = threading.Lock()

    def setup_routes(self):
        """Setup Flask routes"""
        self.app.route('/', methods=['GET'])(self.dashboard)
        self.app.route('/api/tables', methods=['GET'])(self.get_tables)
        self.app.route('/api/table/<table_name>', methods=['GET'])(self.get_table_info)
        self.app.route('/api/query', methods=['POST'])(self.handle_query)
        self.app.route('/api/schema', methods=['GET'])(self.get_schema_overview)
        self.app.route('/api/relationships', methods=['GET'])(self.get_table_relationships)
        self.app.route('/api/query/history', methods=['GET'])(self.get_query_history)
        self.app.route('/api/export/<format>', methods=['POST'])(self.export_results)

    def get_database_connection(self):
        """Get database connection"""
        if not self.engine:
            raise Exception("Database not connected")
        return self.engine.connect()

    def discover_schema(self) -> Dict:
        """Discover and cache database schema"""
        cache_key = f"schema_{self.database_name}"
        
        with self.cache_lock:
            if cache_key in self.schema_cache:
                return self.schema_cache[cache_key]
        
        try:
            schema_info = {
                'database': self.database_name,
                'tables': {},
                'relationships': [],
                'summary': {}
            }
            
            # Get all tables
            table_names = self.inspector.get_table_names()
            
            for table_name in table_names:
                # Get columns
                columns = self.inspector.get_columns(table_name)
                
                # Get primary keys
                pk_constraint = self.inspector.get_pk_constraint(table_name)
                primary_keys = pk_constraint.get('constrained_columns', [])
                
                # Get foreign keys
                foreign_keys = self.inspector.get_foreign_keys(table_name)
                
                # Get sample data
                sample_data = self.get_sample_data(table_name, limit=3)
                
                # Get row count estimate
                row_count = self.get_table_row_count(table_name)
                
                schema_info['tables'][table_name] = {
                    'columns': [
                        {
                            'name': col['name'],
                            'type': str(col['type']),
                            'nullable': col['nullable'],
                            'primary_key': col['name'] in primary_keys,
                            'sample_values': [str(val) for val in sample_data.get(col['name'], [])[:3] if pd.notna(val)]
                        }
                        for col in columns
                    ],
                    'foreign_keys': foreign_keys,
                    'row_count': row_count,
                    'sample_data': sample_data.to_dict(orient='records')[:3] if not sample_data.empty else []
                }
                
                # Collect relationships
                for fk in foreign_keys:
                    schema_info['relationships'].append({
                        'from_table': table_name,
                        'from_columns': fk['constrained_columns'],
                        'to_table': fk['referred_table'],
                        'to_columns': fk['referred_columns']
                    })
            
            # Generate summary
            schema_info['summary'] = {
                'total_tables': len(table_names),
                'total_relationships': len(schema_info['relationships']),
                'largest_tables': sorted(
                    [(name, info['row_count']) for name, info in schema_info['tables'].items()],
                    key=lambda x: x[1] or 0,
                    reverse=True
                )[:5]
            }
            
            # Cache the result
            with self.cache_lock:
                self.schema_cache[cache_key] = schema_info
                
            logger.info(f"Schema discovery completed: {len(table_names)} tables found")
            return schema_info
            
        except Exception as e:
            logger.error(f"Schema discovery failed: {e}")
            return {'error': str(e)}

    def get_sample_data(self, table_name: str, limit: int = 3) -> pd.DataFrame:
        """Get sample data from a table"""
        try:
            with self.get_database_connection() as conn:
                query = f"SELECT TOP {limit} * FROM [{table_name}]"
                return pd.read_sql(query, conn)
        except Exception as e:
            logger.warning(f"Could not get sample data for {table_name}: {e}")
            return pd.DataFrame()

    def get_table_row_count(self, table_name: str) -> Optional[int]:
        """Get approximate row count for a table"""
        try:
            with self.get_database_connection() as conn:
                # Use sys.dm_db_partition_stats for faster approximate count
                query = """
                SELECT SUM(row_count) as row_count
                FROM sys.dm_db_partition_stats 
                WHERE object_id = OBJECT_ID(?) AND index_id IN (0,1)
                """
                result = conn.execute(text(query), (table_name,))
                row = result.fetchone()
                return row[0] if row and row[0] else 0
        except Exception:
            # Fallback to COUNT(*) if stats not available
            try:
                with self.get_database_connection() as conn:
                    query = f"SELECT COUNT(*) FROM [{table_name}]"
                    result = conn.execute(text(query))
                    return result.scalar()
            except Exception as e:
                logger.warning(f"Could not get row count for {table_name}: {e}")
                return None

    def validate_sql_query(self, sql: str) -> bool:
        """Enhanced SQL injection prevention for SQL Server"""
        sql_lower = sql.lower().strip()
        
        # Block dangerous SQL keywords
        dangerous_keywords = [
            'drop', 'delete', 'insert', 'update', 'alter', 'create', 'truncate',
            'exec', 'execute', 'sp_', 'xp_', '--', ';--', '/*', '*/',
            'shutdown', 'backup', 'restore', 'bulk', 'openrowset', 'opendatasource'
        ]
        
        for keyword in dangerous_keywords:
            if keyword in sql_lower:
                logger.warning(f"Dangerous keyword '{keyword}' detected in query")
                return False
                
        # Must start with SELECT or WITH (for CTEs)
        if not (sql_lower.startswith('select') or sql_lower.startswith('with')):
            return False
            
        return True

    def generate_supply_chain_query(self, question: str, schema_info: Dict) -> Dict:
        """Generate SQL query with supply chain business context"""
        try:
            if not self.anthropic_client:
                return {
                    "sql": "",
                    "explanation": "Claude AI is not available. Please check your API configuration.",
                    "chart_type": "table",
                    "insights": "AI features are currently unavailable."
                }
            
            # Build comprehensive context
            context = self.build_query_context(schema_info, question)
            
            prompt = f"""You are a supply chain data analyst expert working with a SQL Server database. 

BUSINESS CONTEXT:
- Domain: Supply Chain Management
- Database: {self.database_name}
- Company: {self.company_name}
- Common KPIs: inventory turnover, fill rate, lead time, on-time delivery, supplier performance

DATABASE SCHEMA:
{context['schema_summary']}

TABLE RELATIONSHIPS:
{context['relationships_summary']}

SAMPLE DATA CONTEXT:
{context['sample_context']}

USER QUESTION: "{question}"

SUPPLY CHAIN ANALYSIS GUIDELINES:
1. Consider time-based analysis for trends and seasonality
2. Include relevant KPIs and performance metrics
3. Think about supplier, product, customer, and inventory perspectives
4. Consider operational efficiency and cost optimization
5. Look for patterns in delivery performance, quality, and costs

TECHNICAL REQUIREMENTS:
1. Generate ONLY a SQL Server compatible SELECT query
2. Use proper table/column names as shown in schema
3. Include appropriate JOINs if analyzing multiple entities
4. Add proper filters and aggregations for meaningful insights
5. Limit results to {self.max_result_rows} rows maximum
6. Use TOP instead of LIMIT for SQL Server
7. Format dates appropriately for analysis

CRITICAL JSON FORMATTING:
- Return ONLY a valid JSON object
- Escape all newlines in SQL as \\n
- Escape all quotes as \\"
- Do not include any text outside the JSON
- Ensure all strings are properly escaped

Return a JSON object with these exact keys:
- "sql": The SQL query (escape newlines as \\n)
- "explanation": Clear business explanation
- "chart_type": Best visualization type
- "insights": Supply chain insights
- "kpis": Array of relevant KPIs

Example format:
{{"sql": "SELECT Column1, COUNT(*) as Count\\nFROM TableName\\nGROUP BY Column1", "explanation": "This shows...", "chart_type": "bar", "insights": "Key insights...", "kpis": ["kpi1", "kpi2"]}}"""
            
            # Make API call to Claude
            response = self.anthropic_client.messages.create(
                model=self.model_name,
                max_tokens=1500,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}]
            )
            
            result_text = response.content[0].text.strip()
            
            # Clean up and parse response
            result = self.parse_claude_response(result_text)
            
            # Validate the SQL
            if not self.validate_sql_query(result.get('sql', '')):
                return {
                    "sql": "",
                    "explanation": "Generated query contains unsafe operations.",
                    "chart_type": "table",
                    "insights": "Query validation failed for security reasons.",
                    "kpis": []
                }
            
            return result
            
        except Exception as e:
            logger.error(f"Error generating supply chain query: {e}")
            return {
                "sql": "",
                "explanation": f"Error generating query: {str(e)}",
                "chart_type": "table",
                "insights": "Failed to generate analysis.",
                "kpis": []
            }

    def build_query_context(self, schema_info: Dict, question: str) -> Dict:
        """Build comprehensive context for Claude"""
        # Schema summary
        schema_summary = []
        for table_name, table_info in schema_info.get('tables', {}).items():
            columns_info = []
            for col in table_info['columns']:
                col_desc = f"{col['name']} ({col['type']}"
                if col['primary_key']:
                    col_desc += ", PK"
                if not col['nullable']:
                    col_desc += ", NOT NULL"
                col_desc += ")"
                if col['sample_values']:
                    col_desc += f" - samples: {', '.join(col['sample_values'][:3])}"
                columns_info.append(col_desc)
            
            row_count_info = f" ({table_info['row_count']} rows)" if table_info['row_count'] else ""
            schema_summary.append(f"Table: {table_name}{row_count_info}\n  Columns: {chr(10).join(['    ' + col for col in columns_info])}")
        
        # Relationships summary
        relationships = schema_info.get('relationships', [])
        relationships_summary = []
        for rel in relationships:
            rel_desc = f"{rel['from_table']}.{','.join(rel['from_columns'])} -> {rel['to_table']}.{','.join(rel['to_columns'])}"
            relationships_summary.append(rel_desc)
        
        # Sample data context for understanding data patterns
        sample_context = []
        for table_name, table_info in schema_info.get('tables', {}).items():
            if table_info.get('sample_data'):
                sample_context.append(f"{table_name}: {len(table_info['sample_data'])} sample records available")
        
        return {
            'schema_summary': '\n\n'.join(schema_summary),
            'relationships_summary': '\n'.join(relationships_summary) if relationships_summary else "No foreign key relationships detected",
            'sample_context': '\n'.join(sample_context)
        }

    def parse_claude_response(self, result_text: str) -> Dict:
        """Parse Claude's response and extract JSON"""
        # Clean up the response
        if result_text.startswith('```json'):
            result_text = result_text[7:]
        if result_text.endswith('```'):
            result_text = result_text[:-3]
        
        # Sometimes Claude wraps JSON in extra text, try to extract it
        if '{' in result_text and '}' in result_text:
            start = result_text.find('{')
            end = result_text.rfind('}') + 1
            result_text = result_text[start:end]
        
        try:
            # First try normal JSON parsing
            result = json.loads(result_text)
            
        except json.JSONDecodeError:
            try:
                # If that fails, try to fix common JSON issues
                # Fix unescaped newlines and tabs in JSON strings
                import re
                
                # Replace unescaped newlines in string values with \\n
                def fix_json_string(match):
                    content = match.group(1)
                    # Escape newlines, tabs, and other control characters
                    content = content.replace('\n', '\\n')
                    content = content.replace('\r', '\\r')
                    content = content.replace('\t', '\\t')
                    content = content.replace('\b', '\\b')
                    content = content.replace('\f', '\\f')
                    return f'"{content}"'
                
                # Find all string values in JSON and fix them
                fixed_text = re.sub(r'"([^"]*(?:\\.[^"]*)*)"', fix_json_string, result_text)
                
                result = json.loads(fixed_text)
                
            except json.JSONDecodeError as e:
                logger.error(f"Could not fix JSON format: {e}")
                # As a last resort, try to extract key information manually
                result = self.extract_json_manually(result_text)
        
        # Ensure all required keys exist
        required_keys = ['sql', 'explanation', 'chart_type', 'insights']
        for key in required_keys:
            if key not in result:
                result[key] = ""
        
        # Ensure kpis is a list
        if 'kpis' not in result:
            result['kpis'] = []
        
        return result

    def extract_json_manually(self, text: str) -> Dict:
        """Manually extract JSON components as fallback"""
        import re
        
        result = {
            "sql": "",
            "explanation": "",
            "chart_type": "table",
            "insights": "",
            "kpis": []
        }
        
        try:
            # Extract SQL query
            sql_match = re.search(r'"sql":\s*"([^"]+(?:\\.[^"]*)*)"', text, re.DOTALL)
            if sql_match:
                sql_content = sql_match.group(1)
                # Unescape the SQL
                sql_content = sql_content.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"')
                result["sql"] = sql_content
            
            # Extract explanation
            exp_match = re.search(r'"explanation":\s*"([^"]+(?:\\.[^"]*)*)"', text, re.DOTALL)
            if exp_match:
                result["explanation"] = exp_match.group(1).replace('\\"', '"')
            
            # Extract chart_type
            chart_match = re.search(r'"chart_type":\s*"([^"]+)"', text)
            if chart_match:
                result["chart_type"] = chart_match.group(1)
            
            # Extract insights
            insights_match = re.search(r'"insights":\s*"([^"]+(?:\\.[^"]*)*)"', text, re.DOTALL)
            if insights_match:
                result["insights"] = insights_match.group(1).replace('\\"', '"')
            
            # Extract KPIs array
            kpis_match = re.search(r'"kpis":\s*\[([^\]]*)\]', text)
            if kpis_match:
                kpis_content = kpis_match.group(1)
                # Extract individual KPI strings
                kpi_items = re.findall(r'"([^"]+)"', kpis_content)
                result["kpis"] = kpi_items
            
        except Exception as e:
            logger.error(f"Manual JSON extraction failed: {e}")
        
        return result

    def execute_query(self, sql: str) -> Tuple[pd.DataFrame, Dict]:
        """Execute SQL query with performance monitoring"""
        start_time = datetime.now()
        
        try:
            # Check if database connection is available
            if not self.engine:
                raise Exception("Database connection not available. Please check your SQL Server configuration.")
            
            # Check cache first
            query_hash = hashlib.md5(sql.encode()).hexdigest()
            with self.cache_lock:
                if query_hash in self.query_cache:
                    logger.info(f"Query result retrieved from cache: {query_hash[:8]}")
                    return self.query_cache[query_hash]
            
            with self.get_database_connection() as conn:
                # Set query timeout
                if hasattr(conn, 'execution_options'):
                    conn = conn.execution_options(autocommit=True)
                
                # Execute query
                df = pd.read_sql(sql, conn)
                
                # Limit result size for performance
                if len(df) > self.max_result_rows:
                    df = df.head(self.max_result_rows)
                    truncated = True
                else:
                    truncated = False
                
                execution_time = (datetime.now() - start_time).total_seconds()
                
                metadata = {
                    'execution_time': execution_time,
                    'row_count': len(df),
                    'truncated': truncated,
                    'columns': list(df.columns),
                    'query_hash': query_hash
                }
                
                # Cache successful results
                result = (df, metadata)
                with self.cache_lock:
                    self.query_cache[query_hash] = result
                
                logger.info(f"Query executed successfully: {len(df)} rows in {execution_time:.2f}s")
                return result
                
        except Exception as e:
            execution_time = (datetime.now() - start_time).total_seconds()
            logger.error(f"Query execution failed after {execution_time:.2f}s: {e}")
            raise

    def create_advanced_visualization(self, df: pd.DataFrame, chart_type: str, question: str, kpis: List[str] = None) -> str:
        """Create supply chain specific visualizations"""
        try:
            if df.empty:
                fig = go.Figure()
                fig.add_annotation(
                    text="No data found for this query",
                    xref="paper", yref="paper",
                    x=0.5, y=0.5,
                    showarrow=False,
                    font=dict(size=16)
                )
                fig.update_layout(title=f"No Results: {question}")
                return json.dumps(fig, cls=PlotlyJSONEncoder)
            
            # Clean column names for better display
            display_df = df.copy()
            display_df.columns = [col.replace('_', ' ').title() for col in display_df.columns]
            
            # Initialize fig variable to avoid UnboundLocalError
            fig = None
            
            # Create visualization based on chart type and data
            if chart_type == "bar" and len(display_df.columns) >= 2:
                fig = px.bar(
                    display_df, 
                    x=display_df.columns[0], 
                    y=display_df.columns[1],
                    title=f"Bar Chart: {question}",
                    labels={display_df.columns[1]: display_df.columns[1]}
                )
                
            elif chart_type == "line" and len(display_df.columns) >= 2:
                fig = px.line(
                    display_df,
                    x=display_df.columns[0],
                    y=display_df.columns[1],
                    title=f"Trend Analysis: {question}",
                    markers=True
                )
                
            elif chart_type == "pie" and len(display_df.columns) >= 2:
                fig = px.pie(
                    display_df,
                    names=display_df.columns[0],
                    values=display_df.columns[1],
                    title=f"Distribution: {question}"
                )
                
            elif chart_type == "scatter" and len(display_df.columns) >= 2:
                color_col = display_df.columns[2] if len(display_df.columns) > 2 else None
                fig = px.scatter(
                    display_df,
                    x=display_df.columns[0],
                    y=display_df.columns[1],
                    color=color_col,
                    title=f"Correlation Analysis: {question}",
                    size_max=20
                )
                
            elif chart_type == "heatmap" and len(display_df.columns) >= 3:
                # Create pivot table for heatmap
                try:
                    pivot_df = display_df.pivot_table(
                        index=display_df.columns[0],
                        columns=display_df.columns[1],
                        values=display_df.columns[2],
                        aggfunc='mean'
                    )
                    fig = px.imshow(
                        pivot_df,
                        title=f"Heatmap Analysis: {question}",
                        aspect="auto"
                    )
                except Exception as e:
                    logger.warning(f"Could not create heatmap: {e}, falling back to table")
                    fig = None  # Will fall through to table creation
                    
            elif chart_type in ["stacked_bar", "grouped_bar"] and len(display_df.columns) >= 3:
                # Create grouped/stacked bar chart
                color_col = display_df.columns[2] if len(display_df.columns) > 2 else None
                fig = px.bar(
                    display_df,
                    x=display_df.columns[0],
                    y=display_df.columns[1],
                    color=color_col,
                    title=f"Grouped Analysis: {question}",
                    barmode='group' if chart_type == "grouped_bar" else 'stack'
                )
            
            # If no specific chart was created or chart_type is "table", create table
            if fig is None or chart_type == "table" or len(display_df.columns) < 2:
                # Enhanced table view
                fig = go.Figure(data=[go.Table(
                    header=dict(
                        values=list(display_df.columns),
                        fill_color='#4f46e5',
                        font=dict(color='white', size=12),
                        align='left'
                    ),
                    cells=dict(
                        values=[display_df[col] for col in display_df.columns],
                        fill_color='#f8fafc',
                        align='left',
                        font=dict(size=11)
                    )
                )])
                fig.update_layout(title=f"Data Table: {question}")
            
            # Enhanced layout for supply chain context
            fig.update_layout(
                title_font_size=16,
                height=500,
                margin=dict(l=50, r=50, t=80, b=50),
                template="plotly_white",
                showlegend=True if chart_type in ["pie", "scatter", "grouped_bar", "stacked_bar"] else False
            )
            
            # Add KPI annotations if available
            if kpis and chart_type != "table":
                kpi_text = f"KPIs: {', '.join(kpis)}"
                fig.add_annotation(
                    text=kpi_text,
                    xref="paper", yref="paper",
                    x=0, y=1.05,
                    showarrow=False,
                    font=dict(size=10, color="gray"),
                    align="left"
                )
            
            return json.dumps(fig, cls=PlotlyJSONEncoder)
            
        except Exception as e:
            logger.error(f"Visualization error: {e}")
            # Return error visualization
            fig = go.Figure()
            fig.add_annotation(
                text=f"Visualization error: {str(e)}",
                xref="paper", yref="paper",
                x=0.5, y=0.5,
                showarrow=False,
                font=dict(size=14, color="red")
            )
            fig.update_layout(title="Visualization Error")
            return json.dumps(fig, cls=PlotlyJSONEncoder)

    # Flask route handlers
    def dashboard(self):
        """Render the main dashboard"""
        try:
            # Get basic schema info for the sidebar
            schema_info = self.discover_schema()
            
            if 'error' in schema_info:
                # Database connection failed - show error dashboard
                return render_template_string(
                    DATABASE_ERROR_TEMPLATE,
                    company_name=self.company_name,
                    database_name=self.database_name,
                    error_message=schema_info['error'],
                    business_domain=self.business_domain.replace('_', ' ').title()
                )
            else:
                total_tables = len(schema_info.get('tables', {}))
                largest_tables = schema_info.get('summary', {}).get('largest_tables', [])
                tables_summary = f"{total_tables} tables available"
                
                return render_template_string(
                    ENTERPRISE_DASHBOARD_TEMPLATE,
                    company_name=self.company_name,
                    database_name=self.database_name,
                    tables_summary=tables_summary,
                    total_tables=total_tables,
                    business_domain=self.business_domain.replace('_', ' ').title()
                )
                
        except Exception as e:
            logger.error(f"Dashboard error: {e}")
            return render_template_string(
                ERROR_TEMPLATE,
                error_message=str(e)
            )

    def get_tables(self):
        """Get list of available tables"""
        try:
            schema_info = self.discover_schema()
            if 'error' in schema_info:
                return jsonify({"error": schema_info['error']}), 500
                
            tables = []
            for table_name, table_info in schema_info['tables'].items():
                tables.append({
                    'name': table_name,
                    'row_count': table_info['row_count'],
                    'column_count': len(table_info['columns']),
                    'has_relationships': len(table_info['foreign_keys']) > 0
                })
            
            return jsonify({"tables": tables})
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    def get_table_info(self, table_name):
        """Get detailed information about a specific table"""
        try:
            schema_info = self.discover_schema()
            if table_name not in schema_info.get('tables', {}):
                return jsonify({"error": "Table not found"}), 404
                
            table_info = schema_info['tables'][table_name]
            
            # Get more detailed sample data
            sample_df = self.get_sample_data(table_name, limit=10)
            
            return jsonify({
                "table_name": table_name,
                "columns": table_info['columns'],
                "row_count": table_info['row_count'],
                "foreign_keys": table_info['foreign_keys'],
                "sample_data": sample_df.to_dict(orient='records') if not sample_df.empty else []
            })
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    def handle_query(self):
        """Process natural language query"""
        try:
            data = request.get_json()
            question = data.get('query', '').strip()
            
            if not question:
                return jsonify({"error": "No query provided"}), 400
            
            # Check if database is connected
            if not self.engine:
                return jsonify({
                    "error": "Database connection not available",
                    "details": "Cannot execute queries without a database connection. Please check your SQL Server configuration.",
                    "suggestions": [
                        "Verify SQL Server is running",
                        "Check connection string in .env file",
                        "Test connection with SQL Server Management Studio",
                        "Run troubleshoot.py for detailed diagnostics"
                    ]
                }), 503  # Service Unavailable
            
            # Log the query
            logger.info(f"Processing query: {question}")
            
            # Get schema information
            schema_info = self.discover_schema()
            if 'error' in schema_info:
                return jsonify({
                    "error": "Database schema unavailable",
                    "details": schema_info['error'],
                    "suggestions": [
                        "Check database connection",
                        "Verify database permissions",
                        "Ensure database contains tables"
                    ]
                }), 503
            
            # Generate SQL using Claude with supply chain context
            ai_response = self.generate_supply_chain_query(question, schema_info)
            sql_query = ai_response.get('sql', '')
            
            if not sql_query:
                return jsonify({
                    "error": "Could not generate SQL query",
                    "explanation": ai_response.get('explanation', 'Unknown error'),
                    "suggestions": [
                        "Try rephrasing your question",
                        "Be more specific about what you want to analyze",
                        "Check if you're referring to the correct data entities"
                    ]
                }), 400
            
            # Execute the query
            try:
                df, query_metadata = self.execute_query(sql_query)
                
                if df.empty:
                    return jsonify({
                        "insight": "No data matches your query criteria.",
                        "explanation": ai_response.get('explanation', ''),
                        "sql": sql_query,
                        "chart_json": self.create_advanced_visualization(df, "table", question),
                        "data_preview": [],
                        "row_count": 0,
                        "execution_time": query_metadata['execution_time'],
                        "kpis": ai_response.get('kpis', [])
                    })
                
                # Generate visualization
                chart_type = ai_response.get('chart_type', 'table')
                kpis = ai_response.get('kpis', [])
                chart_json = self.create_advanced_visualization(df, chart_type, question, kpis)
                
                # Create data preview
                preview_df = df.head(20)
                data_preview = preview_df.to_dict(orient='records')
                
                # Generate business insight
                insights = ai_response.get('insights', 'Analysis completed successfully.')
                row_count = len(df)
                
                if query_metadata['truncated']:
                    insight = f"Found {self.max_result_rows}+ results (showing first {self.max_result_rows}): {insights}"
                elif row_count == 1:
                    insight = f"Found 1 result: {insights}"
                else:
                    insight = f"Found {row_count} results: {insights}"
                
                # Store query in history
                self.store_query_history(question, sql_query, row_count, query_metadata['execution_time'])
                
                return jsonify({
                    "insight": insight,
                    "explanation": ai_response.get('explanation', ''),
                    "sql": sql_query,
                    "chart_json": chart_json,
                    "data_preview": data_preview,
                    "row_count": row_count,
                    "execution_time": query_metadata['execution_time'],
                    "chart_type": chart_type,
                    "kpis": kpis,
                    "truncated": query_metadata['truncated']
                })
                
            except Exception as e:
                logger.error(f"SQL execution error: {e}")
                return jsonify({
                    "error": "Error executing SQL query",
                    "details": str(e),
                    "sql": sql_query,
                    "suggestions": [
                        "Check if the referenced tables and columns exist",
                        "Verify data types in your query conditions",
                        "Try a simpler version of your question",
                        "Ensure database connection is stable"
                    ]
                }), 400
                
        except Exception as e:
            logger.error(f"Query handling error: {e}")
            return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500

    def get_schema_overview(self):
        """Get database schema overview"""
        try:
            schema_info = self.discover_schema()
            if 'error' in schema_info:
                return jsonify({"error": schema_info['error']}), 500
                
            return jsonify({
                "database": schema_info['database'],
                "summary": schema_info['summary'],
                "table_count": len(schema_info['tables']),
                "relationship_count": len(schema_info['relationships'])
            })
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    def get_table_relationships(self):
        """Get table relationships for visualization"""
        try:
            schema_info = self.discover_schema()
            if 'error' in schema_info:
                return jsonify({"error": schema_info['error']}), 500
                
            return jsonify({"relationships": schema_info['relationships']})
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    def store_query_history(self, question: str, sql: str, row_count: int, execution_time: float):
        """Store query in session history"""
        if 'query_history' not in session:
            session['query_history'] = []
        
        history_entry = {
            'timestamp': datetime.now().isoformat(),
            'question': question,
            'sql': sql,
            'row_count': row_count,
            'execution_time': execution_time
        }
        
        session['query_history'].insert(0, history_entry)
        # Keep only last 50 queries
        session['query_history'] = session['query_history'][:50]

    def get_query_history(self):
        """Get query history from session"""
        try:
            history = session.get('query_history', [])
            return jsonify({"history": history})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    def export_results(self, format):
        """Export query results to various formats"""
        try:
            data = request.get_json()
            # Implementation for exporting results
            return jsonify({"message": f"Export to {format} not yet implemented"}), 501
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    def run(self, debug=None, host=None, port=None):
        """Run the Flask application"""
        debug = debug if debug is not None else os.environ.get('FLASK_DEBUG', 'True').lower() == 'true'
        host = host or os.environ.get('HOST', '0.0.0.0')
        port = int(port or os.environ.get('PORT', 5000))
        
        logger.info(f"Starting Supply Chain Analytics Platform on {host}:{port}")
        logger.info(f"Database: {self.database_name}")
        logger.info(f"Business Domain: {self.business_domain}")
        
        self.app.run(debug=debug, host=host, port=port)

# Modern Enterprise Dashboard Template
ENTERPRISE_DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ company_name }} - Supply Chain Analytics</title>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            --primary-color: #2563eb;
            --secondary-color: #f1f5f9;
            --accent-color: #10b981;
            --text-dark: #1e293b;
            --border-color: #e2e8f0;
            --success-color: #059669;
            --warning-color: #d97706;
            --danger-color: #dc2626;
        }
        
        body {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            font-family: 'Inter', 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        
        .main-container {
            background: white;
            border-radius: 20px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
            margin: 1rem;
            overflow: hidden;
            min-height: calc(100vh - 2rem);
        }
        
        .header {
            background: linear-gradient(135deg, var(--primary-color), #3b82f6);
            color: white;
            padding: 2rem;
            position: relative;
            overflow: hidden;
        }
        
        .header::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="20" cy="20" r="2" fill="rgba(255,255,255,0.1)"/><circle cx="80" cy="40" r="1" fill="rgba(255,255,255,0.1)"/><circle cx="40" cy="80" r="1.5" fill="rgba(255,255,255,0.1)"/></svg>');
        }
        
        .header-content {
            position: relative;
            z-index: 1;
        }
        
        .header h1 {
            margin: 0;
            font-size: 2.8rem;
            font-weight: 700;
            background: linear-gradient(45deg, #ffffff, #e0e7ff);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .header .subtitle {
            margin: 0.5rem 0 0 0;
            opacity: 0.95;
            font-size: 1.1rem;
            font-weight: 300;
        }
        
        .stats-bar {
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 12px;
            padding: 1rem;
            margin-top: 1.5rem;
            display: flex;
            gap: 2rem;
            flex-wrap: wrap;
        }
        
        .stat-item {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            color: white;
        }
        
        .stat-value {
            font-weight: 600;
            font-size: 1.2rem;
        }
        
        .content-area {
            display: flex;
            min-height: calc(100vh - 280px);
        }
        
        .sidebar {
            width: 300px;
            background: var(--secondary-color);
            border-right: 1px solid var(--border-color);
            padding: 2rem;
            overflow-y: auto;
        }
        
        .main-content {
            flex: 1;
            padding: 2rem;
            background: #fafafa;
        }
        
        .query-section {
            background: white;
            border-radius: 16px;
            padding: 2rem;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
            margin-bottom: 2rem;
        }
        
        .section-title {
            color: var(--text-dark);
            margin-bottom: 1.5rem;
            font-weight: 600;
            font-size: 1.3rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        
        .query-input {
            position: relative;
        }
        
        .query-textarea {
            width: 100%;
            border-radius: 12px;
            border: 2px solid var(--border-color);
            padding: 1rem;
            font-size: 1rem;
            resize: vertical;
            transition: border-color 0.3s ease;
            background: #fafafa;
        }
        
        .query-textarea:focus {
            outline: none;
            border-color: var(--primary-color);
            box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
            background: white;
        }
        
        .btn-primary-custom {
            background: linear-gradient(135deg, var(--primary-color), #3b82f6);
            border: none;
            border-radius: 12px;
            color: white;
            padding: 0.875rem 2rem;
            font-weight: 600;
            font-size: 1rem;
            transition: all 0.3s ease;
            box-shadow: 0 4px 6px -1px rgba(37, 99, 235, 0.3);
        }
        
        .btn-primary-custom:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 25px -8px rgba(37, 99, 235, 0.5);
        }
        
        .btn-secondary-custom {
            background: #6b7280;
            border: none;
            border-radius: 12px;
            color: white;
            padding: 0.875rem 1.5rem;
            font-weight: 500;
            transition: all 0.3s ease;
        }
        
        .btn-secondary-custom:hover {
            background: #4b5563;
            transform: translateY(-1px);
        }
        
        .loading-spinner {
            display: none;
            text-align: center;
            padding: 3rem;
        }
        
        .spinner {
            border: 4px solid var(--border-color);
            border-top: 4px solid var(--primary-color);
            border-radius: 50%;
            width: 50px;
            height: 50px;
            animation: spin 1s linear infinite;
            margin: 0 auto 1rem;
        }
        
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        
        .results-container {
            background: white;
            border-radius: 16px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
            overflow: hidden;
        }
        
        .insight-header {
            background: linear-gradient(135deg, #f0f9ff, #e0f2fe);
            border-bottom: 1px solid #0ea5e9;
            padding: 1.5rem;
        }
        
        .insight-content {
            padding: 1.5rem;
        }
        
        .sql-display {
            background: #1e293b;
            color: #f1f5f9;
            border-radius: 8px;
            padding: 1rem;
            font-family: 'Fira Code', 'Courier New', monospace;
            font-size: 0.875rem;
            overflow-x: auto;
            margin: 1rem 0;
        }
        
        .chart-container {
            background: white;
            border-radius: 12px;
            padding: 1rem;
            margin-top: 1rem;
            min-height: 400px;
        }
        
        .kpi-badges {
            display: flex;
            gap: 0.5rem;
            flex-wrap: wrap;
            margin: 1rem 0;
        }
        
        .kpi-badge {
            background: var(--accent-color);
            color: white;
            padding: 0.25rem 0.75rem;
            border-radius: 20px;
            font-size: 0.8rem;
            font-weight: 500;
        }
        
        .sidebar-section {
            margin-bottom: 2rem;
        }
        
        .sidebar-section h6 {
            color: var(--text-dark);
            font-weight: 600;
            margin-bottom: 1rem;
            font-size: 0.9rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .table-list {
            list-style: none;
            padding: 0;
        }
        
        .table-list li {
            padding: 0.5rem;
            border-radius: 8px;
            margin-bottom: 0.25rem;
            cursor: pointer;
            transition: background-color 0.2s ease;
            font-size: 0.9rem;
        }
        
        .table-list li:hover {
            background: rgba(37, 99, 235, 0.1);
        }
        
        .error-container {
            background: #fef2f2;
            border: 1px solid #fecaca;
            border-radius: 12px;
            padding: 1.5rem;
            margin: 1rem 0;
        }
        
        .error-title {
            color: var(--danger-color);
            font-weight: 600;
            margin-bottom: 0.5rem;
        }
        
        .suggestions {
            background: #f0f9ff;
            border: 1px solid #bae6fd;
            border-radius: 8px;
            padding: 1rem;
            margin-top: 1rem;
        }
        
        .suggestions h6 {
            color: var(--primary-color);
            margin-bottom: 0.5rem;
        }
        
        .suggestions ul {
            margin: 0;
            padding-left: 1.5rem;
        }
        
        .query-examples {
            background: #fafafa;
            border-radius: 8px;
            padding: 1rem;
            margin-top: 1rem;
        }
        
        .query-examples h6 {
            color: var(--text-dark);
            margin-bottom: 0.75rem;
            font-size: 0.9rem;
        }
        
        .example-query {
            background: white;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 0.75rem;
            margin-bottom: 0.5rem;
            cursor: pointer;
            transition: all 0.2s ease;
            font-size: 0.85rem;
        }
        
        .example-query:hover {
            border-color: var(--primary-color);
            box-shadow: 0 2px 4px rgba(37, 99, 235, 0.1);
        }
        
        .performance-info {
            display: flex;
            gap: 1rem;
            align-items: center;
            margin-top: 0.5rem;
            font-size: 0.85rem;
            color: #6b7280;
        }
        
        .performance-badge {
            background: #f3f4f6;
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
            font-weight: 500;
        }
        
        @media (max-width: 768px) {
            .content-area {
                flex-direction: column;
            }
            
            .sidebar {
                width: 100%;
                border-right: none;
                border-bottom: 1px solid var(--border-color);
            }
            
            .main-container {
                margin: 0.5rem;
                border-radius: 12px;
            }
            
            .header h1 {
                font-size: 2rem;
            }
            
            .stats-bar {
                flex-direction: column;
                gap: 1rem;
            }
        }
    </style>
</head>
<body>
    <div class="main-container">
        <div class="header">
            <div class="header-content">
                <h1><i class="fas fa-chart-line"></i> {{ company_name }}</h1>
                <p class="subtitle">Supply Chain Analytics Platform - Powered by JADA</p>
                
                <div class="stats-bar">
                    <div class="stat-item">
                        <i class="fas fa-database"></i>
                        <div>
                            <div class="stat-value">{{ database_name }}</div>
                            <div style="font-size: 0.8rem; opacity: 0.8;">Database</div>
                        </div>
                    </div>
                    <div class="stat-item">
                        <i class="fas fa-table"></i>
                        <div>
                            <div class="stat-value">{{ total_tables }}</div>
                            <div style="font-size: 0.8rem; opacity: 0.8;">Tables</div>
                        </div>
                    </div>
                    <div class="stat-item">
                        <i class="fas fa-industry"></i>
                        <div>
                            <div class="stat-value">{{ business_domain }}</div>
                            <div style="font-size: 0.8rem; opacity: 0.8;">Domain</div>
                        </div>
                    </div>
                    <div class="stat-item">
                        <i class="fas fa-users"></i>
                        <div>
                            <div class="stat-value">Multi-Team</div>
                            <div style="font-size: 0.8rem; opacity: 0.8;">Access</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="content-area">
            <div class="sidebar">
                <div class="sidebar-section">
                    <h6><i class="fas fa-table"></i> Database Tables</h6>
                    <ul class="table-list" id="tablesList">
                        <li><i class="fas fa-spinner fa-spin"></i> Loading tables...</li>
                    </ul>
                </div>
                
                <div class="sidebar-section">
                    <h6><i class="fas fa-lightbulb"></i> Example Queries</h6>
                    <div class="query-examples">
                        <div class="example-query" onclick="setQuery('What are the top 10 suppliers by order volume this year?')">
                            <i class="fas fa-truck"></i> Top suppliers by volume
                        </div>
                        <div class="example-query" onclick="setQuery('Show inventory levels by product category')">
                            <i class="fas fa-boxes"></i> Inventory by category
                        </div>
                        <div class="example-query" onclick="setQuery('Analyze delivery performance trends over the last 6 months')">
                            <i class="fas fa-chart-line"></i> Delivery performance trends
                        </div>
                        <div class="example-query" onclick="setQuery('Which products have the highest inventory turnover?')">
                            <i class="fas fa-sync-alt"></i> Inventory turnover analysis
                        </div>
                        <div class="example-query" onclick="setQuery('Show cost analysis by supplier and month')">
                            <i class="fas fa-dollar-sign"></i> Cost analysis by supplier
                        </div>
                        <div class="example-query" onclick="setQuery('Find customers with highest order frequency')">
                            <i class="fas fa-user-friends"></i> Customer order frequency
                        </div>
                    </div>
                </div>
                
                <div class="sidebar-section">
                    <h6><i class="fas fa-history"></i> Query History</h6>
                    <div id="queryHistory">
                        <p style="color: #6b7280; font-size: 0.85rem;">No queries yet</p>
                    </div>
                </div>
            </div>
            
            <div class="main-content">
                <div class="query-section">
                    <h3 class="section-title">
                        <i class="fas fa-search"></i> Ask About Your Supply Chain Data
                    </h3>
                    
                    <form id="queryForm">
                        <div class="query-input">
                            <textarea 
                                id="queryTextarea" 
                                class="query-textarea" 
                                rows="4" 
                                placeholder="Ask questions like:&#10;• What are our top-performing suppliers this quarter?&#10;• Show me inventory levels for products with low stock&#10;• Analyze order fulfillment times by region&#10;• Which customers have the highest order values?&#10;• Compare supplier delivery performance month over month"></textarea>
                        </div>
                        
                        <div style="margin-top: 1.5rem; display: flex; gap: 1rem; align-items: center;">
                            <button type="submit" class="btn-primary-custom">
                                <i class="fas fa-brain"></i> Analyze with AI
                            </button>
                            <button type="button" class="btn-secondary-custom" onclick="clearQuery()">
                                <i class="fas fa-eraser"></i> Clear
                            </button>
                            <div style="margin-left: auto; font-size: 0.85rem; color: #6b7280;">
                                Powered by Claude AI
                            </div>
                        </div>
                    </form>
                </div>
                
                <div id="loadingSpinner" class="loading-spinner">
                    <div class="spinner"></div>
                    <p>Claude is analyzing your data...</p>
                </div>
                
                <div id="resultsContainer"></div>
            </div>
        </div>
    </div>
    
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        // Load tables on page load
        document.addEventListener('DOMContentLoaded', function() {
            loadTables();
            loadQueryHistory();
        });
        
        // Query form handler
        document.getElementById('queryForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const query = document.getElementById('queryTextarea').value.trim();
            if (!query) {
                alert('Please enter a question about your data');
                return;
            }
            
            await executeQuery(query);
        });
        
        async function executeQuery(query) {
            showLoading(true);
            document.getElementById('resultsContainer').innerHTML = '';
            
            try {
                const response = await fetch('/api/query', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ query })
                });
                
                const result = await response.json();
                showLoading(false);
                
                if (response.ok) {
                    displayResults(result, query);
                    loadQueryHistory(); // Refresh history
                } else {
                    displayError(result);
                }
            } catch (error) {
                showLoading(false);
                displayError({ error: `Request failed: ${error.message}` });
            }
        }
        
        async function loadTables() {
            try {
                const response = await fetch('/api/tables');
                const data = await response.json();
                
                const tablesList = document.getElementById('tablesList');
                
                if (response.ok && data.tables) {
                    tablesList.innerHTML = data.tables.map(table => 
                        `<li onclick="showTableInfo('${table.name}')">
                            <i class="fas fa-table"></i> ${table.name}
                            <div style="font-size: 0.75rem; color: #6b7280; margin-left: 1.2rem;">
                                ${table.row_count ? table.row_count.toLocaleString() + ' rows' : 'No data'} • 
                                ${table.column_count} columns
                            </div>
                        </li>`
                    ).join('');
                } else {
                    tablesList.innerHTML = '<li style="color: #dc2626;"><i class="fas fa-exclamation-triangle"></i> Error loading tables</li>';
                }
            } catch (error) {
                document.getElementById('tablesList').innerHTML = '<li style="color: #dc2626;"><i class="fas fa-exclamation-triangle"></i> Connection error</li>';
            }
        }
        
        async function loadQueryHistory() {
            try {
                const response = await fetch('/api/query/history');
                const data = await response.json();
                
                const historyContainer = document.getElementById('queryHistory');
                
                if (response.ok && data.history && data.history.length > 0) {
                    historyContainer.innerHTML = data.history.slice(0, 5).map(item => 
                        `<div class="example-query" onclick="setQuery('${item.question.replace(/'/g, '\\\'')}')" 
                              style="margin-bottom: 0.5rem; font-size: 0.8rem;">
                            <div style="font-weight: 500; margin-bottom: 0.25rem;">${item.question.substring(0, 60)}${item.question.length > 60 ? '...' : ''}</div>
                            <div style="color: #6b7280; font-size: 0.7rem;">
                                ${item.row_count} rows • ${item.execution_time.toFixed(2)}s
                            </div>
                        </div>`
                    ).join('');
                } else {
                    historyContainer.innerHTML = '<p style="color: #6b7280; font-size: 0.85rem;">No queries yet</p>';
                }
            } catch (error) {
                console.error('Error loading query history:', error);
            }
        }
        
        function setQuery(query) {
            document.getElementById('queryTextarea').value = query;
            document.getElementById('queryTextarea').focus();
        }
        
        function clearQuery() {
            document.getElementById('queryTextarea').value = '';
            document.getElementById('resultsContainer').innerHTML = '';
        }
        
        function showLoading(show) {
            document.getElementById('loadingSpinner').style.display = show ? 'block' : 'none';
        }
        
        function displayResults(result, query) {
            const container = document.getElementById('resultsContainer');
            
            const kpisBadges = result.kpis && result.kpis.length > 0 ? 
                `<div class="kpi-badges">
                    ${result.kpis.map(kpi => `<span class="kpi-badge">${kpi.replace(/_/g, ' ')}</span>`).join('')}
                </div>` : '';
            
            const performanceInfo = `
                <div class="performance-info">
                    <span class="performance-badge">
                        <i class="fas fa-clock"></i> ${result.execution_time?.toFixed(2) || 0}s
                    </span>
                    <span class="performance-badge">
                        <i class="fas fa-list-ol"></i> ${result.row_count || 0} rows
                    </span>
                    ${result.truncated ? '<span class="performance-badge" style="background: #fef3c7; color: #92400e;"><i class="fas fa-cut"></i> Truncated</span>' : ''}
                    <span class="performance-badge">
                        <i class="fas fa-chart-bar"></i> ${result.chart_type || 'table'}
                    </span>
                </div>
            `;
            
            container.innerHTML = `
                <div class="results-container">
                    <div class="insight-header">
                        <h4 style="margin: 0; color: var(--primary-color);">
                            <i class="fas fa-lightbulb"></i> Analysis Results
                        </h4>
                        ${performanceInfo}
                    </div>
                    
                    <div class="insight-content">
                        <div style="margin-bottom: 1.5rem;">
                            <h5 style="color: var(--text-dark); margin-bottom: 1rem;">
                                <i class="fas fa-chart-pie"></i> Business Insights
                            </h5>
                            <p style="font-size: 1.1rem; line-height: 1.6; margin-bottom: 1rem;">${result.insight}</p>
                            ${kpisBadges}
                        </div>
                        
                        <div style="margin-bottom: 1.5rem;">
                            <h5 style="color: var(--text-dark); margin-bottom: 1rem;">
                                <i class="fas fa-cogs"></i> Analysis Method
                            </h5>
                            <p style="color: #4b5563; line-height: 1.5;">${result.explanation}</p>
                        </div>
                        
                        <div style="margin-bottom: 1.5rem;">
                            <h5 style="color: var(--text-dark); margin-bottom: 1rem;">
                                <i class="fas fa-code"></i> Generated SQL Query
                            </h5>
                            <div class="sql-display">${result.sql}</div>
                        </div>
                    </div>
                </div>
                
                <div class="chart-container">
                    <div id="chartDiv"></div>
                </div>
            `;
            
            // Render the chart
            try {
                const chartData = JSON.parse(result.chart_json);
                Plotly.newPlot('chartDiv', chartData.data, chartData.layout, {
                    responsive: true,
                    displayModeBar: true,
                    modeBarButtonsToRemove: ['pan2d', 'lasso2d']
                });
            } catch (error) {
                document.getElementById('chartDiv').innerHTML = `
                    <div style="text-align: center; padding: 2rem; color: #dc2626;">
                        <i class="fas fa-exclamation-triangle"></i> Chart rendering error: ${error.message}
                    </div>`;
            }
        }
        
        function displayError(result) {
            const container = document.getElementById('resultsContainer');
            
            let html = `
                <div class="error-container">
                    <div class="error-title">
                        <i class="fas fa-exclamation-triangle"></i> Analysis Error
                    </div>
                    <p style="margin-bottom: 1rem;">${result.error}</p>
                    ${result.details ? `<div style="font-size: 0.9rem; color: #6b7280; margin-bottom: 1rem;">Details: ${result.details}</div>` : ''}
                    ${result.sql ? `<div style="margin-bottom: 1rem;"><strong>Generated SQL:</strong><div class="sql-display">${result.sql}</div></div>` : ''}
                </div>
            `;
            
            if (result.suggestions && result.suggestions.length > 0) {
                html += `
                    <div class="suggestions">
                        <h6><i class="fas fa-lightbulb"></i> Suggestions:</h6>
                        <ul>
                            ${result.suggestions.map(s => `<li>${s}</li>`).join('')}
                        </ul>
                    </div>
                `;
            }
            
            container.innerHTML = html;
        }
        
        async function showTableInfo(tableName) {
            try {
                const response = await fetch(`/api/table/${tableName}`);
                const data = await response.json();
                
                if (response.ok) {
                    // Could implement a modal or sidebar panel to show table details
                    console.log('Table info:', data);
                    // For now, just set a query about this table
                    setQuery(`Tell me about the ${tableName} table data`);
                }
            } catch (error) {
                console.error('Error loading table info:', error);
            }
        }
        
        // Auto-resize textarea
        document.getElementById('queryTextarea').addEventListener('input', function() {
            this.style.height = 'auto';
            this.style.height = this.scrollHeight + 'px';
        });
    </script>
</body>
</html>
"""

# Database error template
DATABASE_ERROR_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ company_name }} - Database Connection Error</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        body {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            font-family: 'Inter', 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        
        .main-container {
            background: white;
            border-radius: 20px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
            margin: 2rem auto;
            max-width: 800px;
            overflow: hidden;
        }
        
        .header {
            background: linear-gradient(135deg, #dc2626, #ef4444);
            color: white;
            padding: 2rem;
            text-align: center;
        }
        
        .header h1 {
            margin: 0;
            font-size: 2.5rem;
            font-weight: 700;
        }
        
        .content {
            padding: 2rem;
        }
        
        .error-icon {
            font-size: 4rem;
            color: #dc2626;
            margin-bottom: 1rem;
        }
        
        .solution-card {
            background: #f8f9fa;
            border: 1px solid #e9ecef;
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 1rem;
        }
        
        .solution-title {
            font-weight: 600;
            color: #1e293b;
            margin-bottom: 0.5rem;
        }
        
        .code-block {
            background: #1e293b;
            color: #f1f5f9;
            padding: 1rem;
            border-radius: 8px;
            font-family: monospace;
            font-size: 0.9rem;
            margin: 0.5rem 0;
        }
        
        .btn-primary-custom {
            background: linear-gradient(135deg, #2563eb, #3b82f6);
            border: none;
            border-radius: 8px;
            color: white;
            padding: 0.75rem 1.5rem;
            font-weight: 600;
            text-decoration: none;
            display: inline-block;
            margin: 0.5rem 0;
        }
        
        .btn-primary-custom:hover {
            color: white;
            transform: translateY(-1px);
        }
        
        .status-check {
            background: #fff3cd;
            border: 1px solid #ffeaa7;
            border-radius: 8px;
            padding: 1rem;
            margin: 1rem 0;
        }
    </style>
</head>
<body>
    <div class="main-container">
        <div class="header">
            <h1><i class="fas fa-exclamation-triangle"></i> Database Connection Error</h1>
            <p>{{ company_name }} - Supply Chain Analytics Platform</p>
        </div>
        
        <div class="content">
            <div class="text-center">
                <div class="error-icon">
                    <i class="fas fa-database"></i>
                </div>
                <h3>Cannot Connect to SQL Server</h3>
                <p class="text-muted">{{ error_message }}</p>
            </div>
            
            <div class="status-check">
                <h5><i class="fas fa-info-circle"></i> Current Configuration</h5>
                <ul>
                    <li><strong>Server:</strong> EMMANUEL-J\\EMMSQLSERVER</li>
                    <li><strong>Database:</strong> {{ database_name }}</li>
                    <li><strong>Authentication:</strong> Windows Authentication</li>
                    <li><strong>Domain:</strong> {{ business_domain }}</li>
                </ul>
            </div>
            
            <h4><i class="fas fa-tools"></i> How to Fix This</h4>
            
            <div class="solution-card">
                <div class="solution-title">1. Check SQL Server Service</div>
                <p>Ensure your SQL Server instance is running:</p>
                <ol>
                    <li>Press <kbd>Win + R</kbd>, type <code>services.msc</code></li>
                    <li>Find "SQL Server (EMMSQLSERVER)"</li>
                    <li>Status should be "Running" - if not, right-click and "Start"</li>
                </ol>
            </div>
            
            <div class="solution-card">
                <div class="solution-title">2. Test with SQL Server Management Studio</div>
                <p>Verify your connection works manually:</p>
                <ol>
                    <li>Open SQL Server Management Studio (SSMS)</li>
                    <li>Server name: <code>EMMANUEL-J\\EMMSQLSERVER</code></li>
                    <li>Authentication: Windows Authentication</li>
                    <li>Try to connect and access the Supplychain database</li>
                </ol>
            </div>
            
            <div class="solution-card">
                <div class="solution-title">3. Check SQL Server Configuration</div>
                <p>Enable necessary protocols:</p>
                <ol>
                    <li>Open "SQL Server Configuration Manager"</li>
                    <li>Go to "SQL Server Network Configuration"</li>
                    <li>Enable "Named Pipes" and "TCP/IP" protocols</li>
                    <li>Restart SQL Server service</li>
                </ol>
            </div>
            
            <div class="solution-card">
                <div class="solution-title">4. Alternative Connection Strings</div>
                <p>If the instance name doesn't work, try these in your .env file:</p>
                <div class="code-block">
# Try with localhost
SSMS_CONN_STR=DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost\\EMMSQLSERVER;DATABASE=Supplychain;Trusted_Connection=yes;

# Or try with just computer name (if default instance)
SSMS_CONN_STR=DRIVER={ODBC Driver 17 for SQL Server};SERVER=EMMANUEL-J;DATABASE=Supplychain;Trusted_Connection=yes;

# Or try with IP address
SSMS_CONN_STR=DRIVER={ODBC Driver 17 for SQL Server};SERVER=127.0.0.1\\EMMSQLSERVER;DATABASE=Supplychain;Trusted_Connection=yes;
                </div>
            </div>
            
            <div class="solution-card">
                <div class="solution-title">5. Run Troubleshooting Tools</div>
                <p>Use our built-in diagnostic tools:</p>
                <div class="code-block">
# Run comprehensive troubleshooting
python troubleshoot.py

# Check specific SQL Server connection
python -c "import pyodbc; print('Testing connection...'); conn = pyodbc.connect('DRIVER={ODBC Driver 17 for SQL Server};SERVER=EMMANUEL-J\\\\EMMSQLSERVER;DATABASE=Supplychain;Trusted_Connection=yes;'); print('✅ Connection successful!')"
                </div>
            </div>
            
            <div class="text-center" style="margin-top: 2rem;">
                <a href="javascript:location.reload()" class="btn-primary-custom">
                    <i class="fas fa-sync-alt"></i> Retry Connection
                </a>
                <br>
                <small class="text-muted">Refresh this page after fixing the SQL Server connection</small>
            </div>
            
            <div style="margin-top: 2rem; padding: 1rem; background: #e3f2fd; border-radius: 8px;">
                <h6><i class="fas fa-lightbulb"></i> Pro Tip</h6>
                <p class="mb-0">The easiest way to diagnose this is to first test the connection in SQL Server Management Studio with the exact same server name and authentication method.</p>
            </div>
        </div>
    </div>
</body>
</html>
"""

# Error template
ERROR_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Database Connection Error</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; }
        .error { background: #fee; border: 1px solid #fcc; padding: 20px; border-radius: 5px; }
    </style>
</head>
<body>
    <div class="error">
        <h1>Database Connection Error</h1>
        <p>{{ error_message }}</p>
        <p>Please check your database connection settings and try again.</p>
    </div>
</body>
</html>
"""

# Application factory
def create_app():
    """Create and configure the Flask application"""
    return SupplyChainAnalytics()

if __name__ == "__main__":
    # Check if .env file exists
    if not os.path.exists('.env'):
        logger.warning("No .env file found. Please create one using the provided template.")
        logger.warning("The application may not work correctly without proper configuration.")
    
    app = create_app()
    app.run()