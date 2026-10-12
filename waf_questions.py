"""Auto-generated WAF question metadata.

Source: https://databricks-solutions.github.io/waf-assessment-tool/src/data/waf-data.json
(the question bank behind the Databricks WAF Assessment Tool).

Regenerate with ``python tools/gen_questions.py``.
"""

PILLAR_NAMES = {
    'data-ai-governance': 'Data & AI Governance',
    'interoperability-usability': 'Interoperability & Usability',
    'operational-excellence': 'Operational Excellence',
    'security-compliance-privacy': 'Security, Compliance & Privacy',
    'reliability': 'Reliability',
    'performance-efficiency': 'Performance Efficiency',
    'cost-optimization': 'Cost Optimization',
}

#: question id -> {pillar_id, principle, title}
QUESTIONS = {
    'DG-01-01': {
        "pillar_id": 'data-ai-governance',
        "principle": 'Unify data and AI management',
        "title": 'Establish data governance process',
    },
    'DG-01-02': {
        "pillar_id": 'data-ai-governance',
        "principle": 'Unify data and AI management',
        "title": 'Manage metadata for all data assets in one place',
    },
    'DG-01-03': {
        "pillar_id": 'data-ai-governance',
        "principle": 'Unify data and AI management',
        "title": 'Track data and AI lineage to drive visibility of the data',
    },
    'DG-01-04': {
        "pillar_id": 'data-ai-governance',
        "principle": 'Unify data and AI management',
        "title": 'Add consistent descriptions to your metadata',
    },
    'DG-01-05': {
        "pillar_id": 'data-ai-governance',
        "principle": 'Unify data and AI management',
        "title": 'Allow easy data discovery for data consumers',
    },
    'DG-01-06': {
        "pillar_id": 'data-ai-governance',
        "principle": 'Unify data and AI management',
        "title": 'Govern AI assets together with data',
    },
    'DG-02-01': {
        "pillar_id": 'data-ai-governance',
        "principle": 'Unify data and AI security',
        "title": 'Centralize access control for all data and AI assets',
    },
    'DG-02-02': {
        "pillar_id": 'data-ai-governance',
        "principle": 'Unify data and AI security',
        "title": 'Configure audit logging',
    },
    'DG-02-03': {
        "pillar_id": 'data-ai-governance',
        "principle": 'Unify data and AI security',
        "title": 'Audit data platform events',
    },
    'DG-03-01': {
        "pillar_id": 'data-ai-governance',
        "principle": 'Establish data quality standards',
        "title": 'Define and document data quality standards',
    },
    'DG-03-02': {
        "pillar_id": 'data-ai-governance',
        "principle": 'Establish data quality standards',
        "title": 'Use data quality tools for profiling, cleansing, validating, and monitoring data',
    },
    'DG-03-03': {
        "pillar_id": 'data-ai-governance',
        "principle": 'Establish data quality standards',
        "title": 'Implement and enforce standardized data formats and definitions',
    },
    'IU-01-01': {
        "pillar_id": 'interoperability-usability',
        "principle": 'Define standards for integration',
        "title": 'Use standard and reusable integration patterns for external integration',
    },
    'IU-01-02': {
        "pillar_id": 'interoperability-usability',
        "principle": 'Define standards for integration',
        "title": 'Use optimized connectors to ingest data sources into the lakehouse',
    },
    'IU-01-03': {
        "pillar_id": 'interoperability-usability',
        "principle": 'Define standards for integration',
        "title": 'Use certified partner tools',
    },
    'IU-01-04': {
        "pillar_id": 'interoperability-usability',
        "principle": 'Define standards for integration',
        "title": 'Reduce complexity of data engineering pipelines',
    },
    'IU-02-01': {
        "pillar_id": 'interoperability-usability',
        "principle": 'Utilize open interfaces and data formats',
        "title": 'Use open data formats',
    },
    'IU-02-02': {
        "pillar_id": 'interoperability-usability',
        "principle": 'Utilize open interfaces and data formats',
        "title": 'Enable secure data sharing for all data and AI assets',
    },
    'IU-02-03': {
        "pillar_id": 'interoperability-usability',
        "principle": 'Utilize open interfaces and data formats',
        "title": 'Use open standards for your AI workflows',
    },
    'IU-03-01': {
        "pillar_id": 'interoperability-usability',
        "principle": 'Simplify new use case implementation',
        "title": 'Provide a self-service experience across the platform',
    },
    'IU-03-02': {
        "pillar_id": 'interoperability-usability',
        "principle": 'Simplify new use case implementation',
        "title": 'Use serverless services',
    },
    'IU-03-03': {
        "pillar_id": 'interoperability-usability',
        "principle": 'Simplify new use case implementation',
        "title": 'Use pre-defined compute templates',
    },
    'IU-03-04': {
        "pillar_id": 'interoperability-usability',
        "principle": 'Simplify new use case implementation',
        "title": 'Use AI capabilities to increase productivity',
    },
    'IU-04-01': {
        "pillar_id": 'interoperability-usability',
        "principle": 'Ensure data consistency and usability',
        "title": 'Offer reusable data-as-products that the business can trust',
    },
    'IU-04-02': {
        "pillar_id": 'interoperability-usability',
        "principle": 'Ensure data consistency and usability',
        "title": 'Publish data products semantically consistent across the enterprise',
    },
    'IU-04-03': {
        "pillar_id": 'interoperability-usability',
        "principle": 'Ensure data consistency and usability',
        "title": 'Provide a central catalog for discovery and lineage',
    },
    'OE-01-01': {
        "pillar_id": 'operational-excellence',
        "principle": 'Optimize build and release processes',
        "title": 'Create a dedicated Lakehouse operations team',
    },
    'OE-01-02': {
        "pillar_id": 'operational-excellence',
        "principle": 'Optimize build and release processes',
        "title": 'Use enterprise source code management (SCM)',
    },
    'OE-01-03': {
        "pillar_id": 'operational-excellence',
        "principle": 'Optimize build and release processes',
        "title": 'Standardize DevOps processes (CI/CD)',
    },
    'OE-01-04': {
        "pillar_id": 'operational-excellence',
        "principle": 'Optimize build and release processes',
        "title": 'Standardize MLOps processes across enterprise',
    },
    'OE-01-05': {
        "pillar_id": 'operational-excellence',
        "principle": 'Optimize build and release processes',
        "title": 'Define environment isolation strategy',
    },
    'OE-01-06': {
        "pillar_id": 'operational-excellence',
        "principle": 'Optimize build and release processes',
        "title": 'Streamline the usage and management of various large language model (LLM) providers',
    },
    'OE-01-07': {
        "pillar_id": 'operational-excellence',
        "principle": 'Optimize build and release processes',
        "title": 'Define catalog strategy for your enterprise using Unity Catalog',
    },
    'OE-01-08': {
        "pillar_id": 'operational-excellence',
        "principle": 'Optimize build and release processes',
        "title": 'Compare LLM outputs on set prompts',
    },
    'OE-01-09': {
        "pillar_id": 'operational-excellence',
        "principle": 'Optimize build and release processes',
        "title": 'Build models with all representative, accurate and relevant data sources',
    },
    'OE-02-01': {
        "pillar_id": 'operational-excellence',
        "principle": 'Automate deployments and workloads',
        "title": 'Use Infrastructure as Code for deployments and maintenance',
    },
    'OE-02-02': {
        "pillar_id": 'operational-excellence',
        "principle": 'Automate deployments and workloads',
        "title": 'Standardize compute configurations',
    },
    'OE-02-03': {
        "pillar_id": 'operational-excellence',
        "principle": 'Automate deployments and workloads',
        "title": 'Use automated workflows for jobs',
    },
    'OE-02-04': {
        "pillar_id": 'operational-excellence',
        "principle": 'Automate deployments and workloads',
        "title": 'Use automated and event driven file ingestion',
    },
    'OE-02-05': {
        "pillar_id": 'operational-excellence',
        "principle": 'Automate deployments and workloads',
        "title": 'Use ETL frameworks for data pipelines',
    },
    'OE-02-06': {
        "pillar_id": 'operational-excellence',
        "principle": 'Automate deployments and workloads',
        "title": 'Follow the deploy-code approach for ML workloads',
    },
    'OE-02-07': {
        "pillar_id": 'operational-excellence',
        "principle": 'Automate deployments and workloads',
        "title": 'Use a model registry to decouple code and model lifecycle',
    },
    'OE-02-08': {
        "pillar_id": 'operational-excellence',
        "principle": 'Automate deployments and workloads',
        "title": 'Automate ML experiment tracking',
    },
    'OE-02-09': {
        "pillar_id": 'operational-excellence',
        "principle": 'Automate deployments and workloads',
        "title": 'Reuse the same infrastructure to manage ML pipelines',
    },
    'OE-02-10': {
        "pillar_id": 'operational-excellence',
        "principle": 'Automate deployments and workloads',
        "title": 'Utilize declarative management for complex data and ML pipelines',
    },
    'OE-02-11': {
        "pillar_id": 'operational-excellence',
        "principle": 'Automate deployments and workloads',
        "title": 'Automate LLM evaluation',
    },
    'OE-03-01': {
        "pillar_id": 'operational-excellence',
        "principle": 'Set up Monitoring, Alerting and Logging',
        "title": 'Establish monitoring processes',
    },
    'OE-03-02': {
        "pillar_id": 'operational-excellence',
        "principle": 'Set up Monitoring, Alerting and Logging',
        "title": 'Use native and external tools for platform monitoring',
    },
    'OE-03-03': {
        "pillar_id": 'operational-excellence',
        "principle": 'Set up Monitoring, Alerting and Logging',
        "title": 'Establish an incident response strategy',
    },
    'OE-03-04': {
        "pillar_id": 'operational-excellence',
        "principle": 'Set up Monitoring, Alerting and Logging',
        "title": 'Triggering actions in response to a specific event',
    },
    'OE-04-01': {
        "pillar_id": 'operational-excellence',
        "principle": 'Manage capacity and quotas',
        "title": 'Manage service limits and quotas',
    },
    'OE-04-02': {
        "pillar_id": 'operational-excellence',
        "principle": 'Manage capacity and quotas',
        "title": 'Invest in capacity planning',
    },
    'SCP-01-01': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Manage identity and access using least privilege',
        "title": 'Authenticate via single sign-on.',
    },
    'SCP-01-02': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Manage identity and access using least privilege',
        "title": 'Use multifactor authentication.',
    },
    'SCP-01-03': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Manage identity and access using least privilege',
        "title": 'Disable local passwords.',
    },
    'SCP-01-04': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Manage identity and access using least privilege',
        "title": 'Set complex local passwords.',
    },
    'SCP-01-05': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Manage identity and access using least privilege',
        "title": 'Separate admin accounts from normal user accounts.',
    },
    'SCP-01-06': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Manage identity and access using least privilege',
        "title": 'Use token management.',
    },
    'SCP-01-07': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Manage identity and access using least privilege',
        "title": 'SCIM synchronization of users and groups.',
    },
    'SCP-01-08': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Manage identity and access using least privilege',
        "title": 'Limit cluster creation rights.',
    },
    'SCP-01-09': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Manage identity and access using least privilege',
        "title": 'Use secret management for credentials',
    },
    'SCP-01-10': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Manage identity and access using least privilege',
        "title": 'Configure cloud permissions with least privilege',
    },
    'SCP-01-11': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Manage identity and access using least privilege',
        "title": 'Customer-approved workspace login.',
    },
    'SCP-01-12': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Manage identity and access using least privilege',
        "title": 'Use clusters that support user isolation.',
    },
    'SCP-01-13': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Manage identity and access using least privilege',
        "title": 'Use service principals to run production jobs.',
    },
    'SCP-02-01': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Protect data in transit and at rest',
        "title": 'Avoid storing production data in DBFS.',
    },
    'SCP-02-02': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Protect data in transit and at rest',
        "title": 'Secure access to cloud storage.',
    },
    'SCP-02-03': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Protect data in transit and at rest',
        "title": 'Implement data exfiltration protection',
    },
    'SCP-02-04': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Protect data in transit and at rest',
        "title": 'Use bucket versioning.',
    },
    'SCP-02-05': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Protect data in transit and at rest',
        "title": 'Encrypt storage and restrict access.',
    },
    'SCP-02-06': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Protect data in transit and at rest',
        "title": 'Add a customer-managed key for managed services.',
    },
    'SCP-02-07': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Protect data in transit and at rest',
        "title": 'Configure network-based data exfiltration protection',
    },
    'SCP-03-01': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Secure your network and identify and protect endpoints',
        "title": 'Deploy with a customer-managed VPC or VNet.',
    },
    'SCP-03-02': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Secure your network and identify and protect endpoints',
        "title": 'Configure IP access lists',
    },
    'SCP-03-03': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Secure your network and identify and protect endpoints',
        "title": 'Implement network exfiltration controls',
    },
    'SCP-03-04': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Secure your network and identify and protect endpoints',
        "title": 'Implement private access policies for console',
    },
    'SCP-03-05': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Secure your network and identify and protect endpoints',
        "title": 'Configure VPC endpoint access policies',
    },
    'SCP-03-06': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Secure your network and identify and protect endpoints',
        "title": 'Use Private Link for secure connectivity',
    },
    'SCP-04-01': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Review the Shared Responsibility Model',
        "title": 'Review the Shared Responsibility Model.',
    },
    'SCP-05-01': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Meet compliance and data privacy requirements',
        "title": 'Follow security and compliance guidance',
    },
    'SCP-06-01': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Monitor system security',
        "title": 'Use observability tools like Overwatch',
    },
    'SCP-06-02': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Monitor system security',
        "title": 'Use system tables for audit logs',
    },
    'SCP-06-03': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Monitor system security',
        "title": 'Analyze audit logs regularly',
    },
    'SCP-06-04': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Monitor system security',
        "title": 'Use Enhanced Security Monitoring or Compliance Security Profile',
    },
    'SCP-06-05': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Monitor system security',
        "title": 'Configure tagging to monitor usage and enable charge-back.',
    },
    'SCP-07-01': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Generic controls',
        "title": 'Use AWS Nitro instances.',
    },
    'SCP-07-02': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Generic controls',
        "title": 'Service quotas.',
    },
    'SCP-07-03': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Generic controls',
        "title": 'Leverage CI/CD processes to scan code for hard-coded secrets.',
    },
    'SCP-07-04': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Generic controls',
        "title": 'Isolate sensitive workloads into different workspaces.',
    },
    'SCP-07-05': {
        "pillar_id": 'security-compliance-privacy',
        "principle": 'Generic controls',
        "title": 'Controlling libraries.',
    },
    'R-01-01': {
        "pillar_id": 'reliability',
        "principle": 'Design for failure',
        "title": 'Use a data format that supports ACID transactions',
    },
    'R-01-02': {
        "pillar_id": 'reliability',
        "principle": 'Design for failure',
        "title": 'Use a resilient distributed data engine for all workloads',
    },
    'R-01-03': {
        "pillar_id": 'reliability',
        "principle": 'Design for failure',
        "title": 'Automatically rescue invalid or nonconforming data',
    },
    'R-01-04': {
        "pillar_id": 'reliability',
        "principle": 'Design for failure',
        "title": 'Configure jobs for automatic retries and termination',
    },
    'R-01-05': {
        "pillar_id": 'reliability',
        "principle": 'Design for failure',
        "title": 'Use a scalable and production-grade model serving infrastructure',
    },
    'R-01-06': {
        "pillar_id": 'reliability',
        "principle": 'Design for failure',
        "title": 'Use managed services for your workloads',
    },
    'R-02-01': {
        "pillar_id": 'reliability',
        "principle": 'Manage data quality',
        "title": 'Use a layered storage architecture',
    },
    'R-02-02': {
        "pillar_id": 'reliability',
        "principle": 'Manage data quality',
        "title": 'Improve data integrity by reducing data redundancy',
    },
    'R-02-03': {
        "pillar_id": 'reliability',
        "principle": 'Manage data quality',
        "title": 'Actively manage schemas',
    },
    'R-02-04': {
        "pillar_id": 'reliability',
        "principle": 'Manage data quality',
        "title": 'Use constraints and data expectations',
    },
    'R-02-05': {
        "pillar_id": 'reliability',
        "principle": 'Manage data quality',
        "title": 'Take a data-centric approach to machine learning',
    },
    'R-03-01': {
        "pillar_id": 'reliability',
        "principle": 'Design for autoscaling',
        "title": 'Enable autoscaling for ETL workloads',
    },
    'R-03-02': {
        "pillar_id": 'reliability',
        "principle": 'Design for autoscaling',
        "title": 'Use autoscaling for SQL Warehouses',
    },
    'R-04-01': {
        "pillar_id": 'reliability',
        "principle": 'Test recovery procedures',
        "title": 'Recover from Structured Streaming query failures',
    },
    'R-04-02': {
        "pillar_id": 'reliability',
        "principle": 'Test recovery procedures',
        "title": 'Recover ETL jobs using data time travel capabilities',
    },
    'R-04-03': {
        "pillar_id": 'reliability',
        "principle": 'Test recovery procedures',
        "title": 'Leverage a job automation framework with built-in recovery',
    },
    'R-04-04': {
        "pillar_id": 'reliability',
        "principle": 'Test recovery procedures',
        "title": 'Configure a disaster recovery pattern',
    },
    'R-05-01': {
        "pillar_id": 'reliability',
        "principle": 'Monitor platform events',
        "title": 'Monitor data platform events',
    },
    'R-05-02': {
        "pillar_id": 'reliability',
        "principle": 'Monitor platform events',
        "title": 'Monitor cloud events',
    },
    'PE-01-01': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Utilize serverless capabilities',
        "title": 'Use serverless architecture',
    },
    'PE-01-02': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Utilize serverless capabilities',
        "title": 'Use an enterprise grade model serving service',
    },
    'PE-02-01': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Design workloads for performance',
        "title": 'Understand your data ingestion and access patterns',
    },
    'PE-02-02': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Design workloads for performance',
        "title": 'Use parallel computation where it is beneficial',
    },
    'PE-02-03': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Design workloads for performance',
        "title": 'Analyze the whole chain of execution',
    },
    'PE-02-04': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Design workloads for performance',
        "title": 'Prefer larger clusters',
    },
    'PE-02-05': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Design workloads for performance',
        "title": 'Use native Spark operations',
    },
    'PE-02-06': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Design workloads for performance',
        "title": 'Use native platform engines',
    },
    'PE-02-07': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Design workloads for performance',
        "title": 'Understand your hardware and workload type',
    },
    'PE-02-08': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Design workloads for performance',
        "title": 'Use caching',
    },
    'PE-02-09': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Design workloads for performance',
        "title": 'Use compaction',
    },
    'PE-02-10': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Design workloads for performance',
        "title": 'Use data skipping',
    },
    'PE-02-11': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Design workloads for performance',
        "title": 'Enable Predictive Optimization on your metastore',
    },
    'PE-02-12': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Design workloads for performance',
        "title": 'Avoid over-partitioning',
    },
    'PE-02-14': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Design workloads for performance',
        "title": 'Consider file size tuning',
    },
    'PE-02-15': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Design workloads for performance',
        "title": 'Optimize join performance',
    },
    'PE-02-16': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Design workloads for performance',
        "title": 'Run analyze table to collect table statistics',
    },
    'PE-03-01': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Run performance testing',
        "title": 'Test on data representative of production data',
    },
    'PE-03-02': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Run performance testing',
        "title": 'Take prewarming of resources into account',
    },
    'PE-03-03': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Run performance testing',
        "title": 'Identify bottlenecks',
    },
    'PE-04-01': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Monitor performance',
        "title": 'Monitor query performannce',
    },
    'PE-04-02': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Monitor performance',
        "title": 'Monitor streaming workloads',
    },
    'PE-04-03': {
        "pillar_id": 'performance-efficiency',
        "principle": 'Monitor performance',
        "title": 'Monitor job performance',
    },
    'CO-01-01': {
        "pillar_id": 'cost-optimization',
        "principle": 'Choose optimal resources',
        "title": 'Use performance optimized data formats',
    },
    'CO-01-02': {
        "pillar_id": 'cost-optimization',
        "principle": 'Choose optimal resources',
        "title": 'Use job clusters',
    },
    'CO-01-03': {
        "pillar_id": 'cost-optimization',
        "principle": 'Choose optimal resources',
        "title": 'Use SQL warehouse for SQL workloads',
    },
    'CO-01-04': {
        "pillar_id": 'cost-optimization',
        "principle": 'Choose optimal resources',
        "title": 'Use up-to-date runtimes for your workloads',
    },
    'CO-01-05': {
        "pillar_id": 'cost-optimization',
        "principle": 'Choose optimal resources',
        "title": 'Only use GPUs for the right workloads',
    },
    'CO-01-06': {
        "pillar_id": 'cost-optimization',
        "principle": 'Choose optimal resources',
        "title": 'Use Serverless for your workloads',
    },
    'CO-01-07': {
        "pillar_id": 'cost-optimization',
        "principle": 'Choose optimal resources',
        "title": 'Use the right instance type',
    },
    'CO-01-08': {
        "pillar_id": 'cost-optimization',
        "principle": 'Choose optimal resources',
        "title": 'Choose the most efficient cluster size',
    },
    'CO-01-09': {
        "pillar_id": 'cost-optimization',
        "principle": 'Choose optimal resources',
        "title": 'Evaluate performance optimized query engines',
    },
    'CO-02-01': {
        "pillar_id": 'cost-optimization',
        "principle": 'Dynamically allocate resources',
        "title": 'Leverage auto-scaling compute',
    },
    'CO-02-02': {
        "pillar_id": 'cost-optimization',
        "principle": 'Dynamically allocate resources',
        "title": 'Use auto termination',
    },
    'CO-02-03': {
        "pillar_id": 'cost-optimization',
        "principle": 'Dynamically allocate resources',
        "title": 'Use compute policies to control costs',
    },
    'CO-03-01': {
        "pillar_id": 'cost-optimization',
        "principle": 'Monitor and control cost',
        "title": 'Monitor costs',
    },
    'CO-03-02': {
        "pillar_id": 'cost-optimization',
        "principle": 'Monitor and control cost',
        "title": 'Tag clusters for cost attribution',
    },
    'CO-03-03': {
        "pillar_id": 'cost-optimization',
        "principle": 'Monitor and control cost',
        "title": 'Implement observability to track & chargeback cost',
    },
    'CO-03-04': {
        "pillar_id": 'cost-optimization',
        "principle": 'Monitor and control cost',
        "title": 'Share cost reports regularly',
    },
    'CO-03-05': {
        "pillar_id": 'cost-optimization',
        "principle": 'Monitor and control cost',
        "title": 'Monitor and manage Delta Sharing egress costs',
    },
    'CO-04-01': {
        "pillar_id": 'cost-optimization',
        "principle": 'Design cost-effective workloads',
        "title": 'Balance always-on and triggered streaming',
    },
    'CO-04-02': {
        "pillar_id": 'cost-optimization',
        "principle": 'Design cost-effective workloads',
        "title": 'Balance between on-demand and capacity excess instances',
    },
}



def pillar_questions(pillar_id: str) -> dict:
    """Metadata for every question in a pillar, in source order."""
    return {
        qid: q for qid, q in QUESTIONS.items() if q["pillar_id"] == pillar_id
    }


def pillar_meta(pillar_id: str) -> dict:
    """``{qid: {principle, title}}`` shaped for ``Ctx.run_checks``."""
    return {
        qid: {"principle": q["principle"], "title": q["title"]}
        for qid, q in QUESTIONS.items()
        if q["pillar_id"] == pillar_id
    }
