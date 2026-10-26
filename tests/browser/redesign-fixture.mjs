/** Complete, explicitly synthetic data for all eight dashboard modules. */
export function installRedesignFixture() {
  const epoch = 1792987200;
  const names = ['林舟', '许知夏', '周予安', '陈默', '宋一宁', '顾远'];
  const topics = ['周末的山间徒步', '长期阅读计划', '咖啡口味与睡眠习惯', '合作游戏和朋友', '城市摄影路线', '工作安排与阶段目标'];
  const content = [
    '林舟计划周六早上和朋友去近郊徒步。他希望路线有树荫、总长控制在十公里以内，并在中午之前返回。遇到雨天就改为去图书馆读书。',
    '最近在读《看不见的城市》。他喜欢先完成一章，再把印象最深的段落记在纸质笔记本里；不希望阅读变成每天必须打卡的任务。',
    '平时喜欢低酸的手冲咖啡，下午三点以后会改喝无咖啡因饮品。工作日倾向于早睡，周末偶尔和朋友一起玩合作类游戏。',
    '和朋友玩游戏时更重视配合与聊天，而不是排名。他常常负责整理行动路线，也愿意给第一次参加的人解释规则。',
    '他想在秋天重新走一遍河岸步道，记录傍晚灯光和行人的变化。拍摄时喜欢带轻便相机，不会为了器材改变散步的节奏。',
    '接下来两周先完成手头的文档整理，再安排一次短途旅行。遇到临时任务时，希望保留每天一段不受打扰的阅读时间。',
  ];
  const memories = Array.from({length: 18}, (_, i) => ({
    id: 401 + i, doc_id: `synthetic-document-${i}`, text: content[i % content.length],
    summary: content[i % content.length], metadata: {persona_summary: content[i % content.length],
      memory_type: ['EVENT', 'PREFERENCE', 'FACT'][i % 3], importance: .9 - (i % 5) * .1,
      status: i === 7 ? 'archived' : 'active', create_time: epoch - i * 86400,
      updated_at: epoch - i * 3600, session_id: 'qq:GroupMessage:synthetic-reading',
      memory_origin: i % 2 ? 'conversation' : 'agent_memorize_tool'},
  }));
  const facts = names.flatMap((name, person) => content.slice(0, person === 0 ? 6 : 3).map((value, i) => ({
    identity: `synthetic:qq:${person}`, profile_scope: `synthetic-profile:${person}:${i % 2}`,
    profile_key: `preference_${i}`, display_label: topics[i], category: i === 0 ? 'recent_state' : 'preference',
    value, confidence: .93, source_session_id: `qq:GroupMessage:synthetic-${i % 2}`,
    source_group_id: `synthetic-${i % 2}`, source_group_name: i % 2 ? '河岸读书会' : '周末慢行小组',
    source_sender_name: name, source_memory_id: 401 + i, updated_at: epoch - i * 3600,
    expires_at: null, suppressed: false, revision: 3, current_claim_id: 900 + person * 10 + i,
    quarantined: i === 4 ? 1 : 0, quarantine_reason: i === 4 ? '这条近期计划需要用户再次确认。' : '',
    authority: i === 1 ? 'manual' : 'direct_user', manual_override: i === 1 ? 1 : 0,
    evidence: [{source_memory_id:401+i,quote:value,source_group_name:i%2?'河岸读书会':'周末慢行小组',active:true,created_at:epoch}],
    pending: i === 1 ? [{claim_id:1200+person,value:'最近换成电子阅读器，出门时会更轻便。',state:'pending',can_accept:true,evidence:[{source_memory_id:412,quote:'现在外出会带电子阅读器',source_group_name:'河岸读书会',active:true}]}] : [],
    history: i === 1 ? [{claim_id:800,value:'以前更常在手机上阅读。',state:'superseded',updated_at:epoch-604800,evidence:[]}] : [],
    adoption_history: [],
  })));
  const users = names.map((name, i) => ({
    identity:`synthetic:qq:${i}`, platform:'qq', sender_id:`synthetic-${i}`, sender_name:name,
    manual_alias:i===0?'林舟 · 读书与远行':'', observed_names:[name], pending_count:1,
    last_group_id:'synthetic-reading', last_group_name:'河岸读书会', last_seen_at:epoch-i*7200,
    fact_count:facts.filter(row=>row.identity===`synthetic:qq:${i}`).length, scope_count:2,
    fact_preview:facts.filter(row=>row.identity===`synthetic:qq:${i}`).slice(0,3),
  }));
  const sections = [
    {key:'user_profile',title:'用户档案',hint:'提取稳定偏好，保留来源与采用历史。',fields:[
      {path:'user_profile.enabled',label:'启用用户档案',hint:'从对话中整理用户明确表达的长期偏好。',type:'bool',value:true,default:true},
      {path:'user_profile.scope',label:'档案隔离范围',hint:'按聊天保留事实；同一用户的资料合并展示。',type:'string',value:'session',options:['session','user'],option_labels:['按聊天','按用户']},
      {path:'user_profile.max_facts',label:'每次读取事实数',hint:'限制每次注入的事实数量。',type:'int',value:12,min:1,max:50},
    ]},
    {key:'retrieval',title:'记忆检索',hint:'控制召回数量和等待时间。',fields:[
      {path:'retrieval.timeout',label:'检索超时（秒）',hint:'超时后继续对话，不再等待本次召回。',type:'float',value:5,min:0,max:120},
      {path:'retrieval.enabled',label:'启用混合检索',hint:'同时使用关键词、向量与图谱关系。',type:'bool',value:true,default:true},
      {path:'identity.sessions',label:'独立会话',hint:'使用 JSON 数组配置独立范围。',type:'list',value:['synthetic-reading']},
    ]},
  ];
  const prompts = [
    {id:'memory_summary',category:'memory_processing',name:'记忆整理',name_en:'Memory summary',description:'把对话整理成清楚、可追溯的记忆，保留用户原意与必要时间线索。',variables:['conversation','persona_name'],usage_note:'保持输出字段结构，避免把推测写成事实。',is_custom:true},
    {id:'profile_extraction',category:'user_profile',name:'用户档案提取',name_en:'Profile extraction',description:'识别称呼、长期偏好和明确计划，区分用户直接表达与不确定推断。',variables:['conversation','existing_profile'],usage_note:'仅提取对未来交流有帮助的信息。',is_custom:false},
    {id:'recall_context',category:'retrieval',name:'召回上下文',name_en:'Recall context',description:'选择与当前问题相关的记忆，用简短的上下文帮助自然回应。',variables:['query','memories'],usage_note:'不要重复无关的背景信息。',is_custom:false},
  ];
  const graph = {enabled:true,mode:'overview',summary:{node_type_breakdown:{person:3,topic:5,fact:4,summary:2}},snapshot:{
    nodes:[{id:1,type:'person',label:'林舟',weight:1.8},{id:2,type:'person',label:'许知夏',weight:1.2},{id:3,type:'person',label:'周予安',weight:1},
      ...topics.slice(0,5).map((label,i)=>({id:4+i,type:'topic',label,weight:1.2})),
      ...['喜欢手冲咖啡','保留阅读时间','周末合作游戏','希望路线有树荫'].map((label,i)=>({id:9+i,type:'fact',label,weight:.8})),
      {id:13,type:'summary',label:'十月的阅读与出行',weight:1.3},{id:14,type:'summary',label:'朋友之间的日常',weight:1.1}],
    edges:[{source:1,target:4},{source:1,target:5},{source:1,target:6},{source:2,target:5},{source:3,target:7},{source:4,target:12},{source:5,target:10},{source:6,target:9},{source:7,target:11},{source:1,target:13},{source:2,target:14},{source:3,target:14},{source:8,target:13}].map((edge,i)=>({...edge,id:i+1,type:'related',weight:1})),
    memories:memories.slice(0,6).map(memory=>({memory_id:memory.id,summary:memory.summary})),entries:[],
  }};
  const stats = {total_memories:1286,graph_nodes:342,graph_edges:716,atom_count:2844,sessions:{reading:813,walking:473},
    status_breakdown:{active:1153,archived:117,deleted:16}, importance_distribution:{'0-1':12,'1-2':32,'2-3':51,'3-4':86,'4-5':109,'5-6':261,'6-7':301,'7-8':214,'8-9':130,'9-10':90},
    atom_breakdown:{FACTUAL:940,EPISODIC:842,RELATIONAL:768,PLANNED:294},
    recent_sessions:[{session_id:'qq:GroupMessage:synthetic-reading',message_count:813,last_active:'2026-10-26 15:20'},{session_id:'qq:GroupMessage:synthetic-walking',message_count:473,last_active:'2026-10-25 21:08'}],
  };
  window.redesignCalls = [];
  window.AstrBotPluginPage = {
    ready:async()=>({isDark:new URLSearchParams(location.search).get('theme')==='dark',locale:'zh-CN'}),
    getContext:()=>({isDark:new URLSearchParams(location.search).get('theme')==='dark',locale:'zh-CN'}),
    apiGet:async(path,params={})=>{
      window.redesignCalls.push({method:'GET',path,params});
      if(path==='page/stats')return stats;
      if(path==='page/graph/overview')return graph;
      if(path==='page/memories')return {items:memories,total:memories.length,has_more:false};
      if(path==='page/memories/detail'){const row=memories.find(row=>row.id===Number(params.memory_id))||memories[0];return {memory_id:row.id,summary:row.summary,content:row.text,importance:.9,status:'active',memory_origin:'agent_memorize_tool',memorize_reason:'用户明确希望保留这段安排。',graph_context:graph.snapshot};}
      if(path==='page/profiles/users')return {users,total:users.length,limit:30,offset:0,has_more:false,profile_enabled:true,scope_mode:'session'};
      if(path==='page/profiles'){const items=facts.filter(row=>row.identity===params.identity);return {items,total:items.length,limit:500,offset:0,has_more:false,profile_enabled:true,scope_mode:'session'};}
      if(path==='page/config')return {sections,config:{user_profile:{enabled:true,scope:'session'},retrieval:{timeout:5}},reload_required:true};
      if(path==='page/prompts')return {prompts,categories:[{id:'memory_processing',name:'记忆整理',name_en:'Memory processing'},{id:'user_profile',name:'用户档案',name_en:'User profiles'},{id:'retrieval',name:'记忆召回',name_en:'Retrieval'}]};
      if(path.startsWith('page/prompts/'))return {...prompts[0],content:'请根据以下对话整理可靠的记忆。\n\n{conversation}\n\n保留用户的原意，标明相关时间。',default_content:'{conversation}'};
      if(path==='page/backups')return {backups:[{name:'anamnesis_2026-10-26_1420',backup_timestamp:'2026-10-26 14:20',backup_unix_time:epoch,file_count:8},{name:'anamnesis_2026-10-25_1420',backup_timestamp:'2026-10-25 14:20',backup_unix_time:epoch-86400,file_count:8}]};
      if(path==='page/consolidation/status')return {config:{enabled:true,trigger:'daily',granularity:'semantic',keep_original:'archive'},consolidated_count:47,archived_count:117};
      if(path==='page/operations')return [{id:'synthetic-import-october',kind:'import',status:'completed',created_at:epoch},{id:'synthetic-correction-401',kind:'correction',status:'preview',created_at:epoch-3600},{id:'synthetic-deletion-archive',kind:'delete',status:'completed',created_at:epoch-7200}];
      if(path==='page/episodes')return [{id:'synthetic-weekend',title:'周末慢行与阅读安排',summary:content[0],session_id:'qq:GroupMessage:synthetic-reading',source_session_id:'qq:GroupMessage:synthetic-reading',persona_id:'synthetic-persona',memory_ids:[401,402],revision:1}];
      if(path==='page/projection')return [{id:'synthetic-projection-1'},{id:'synthetic-projection-2'}];
      throw Error(`Unexpected synthetic GET: ${path}`);
    },
    apiPost:async(path,body={})=>{
      window.redesignCalls.push({method:'POST',path,body});
      if(path==='page/recall/test')return {elapsed_time_ms:128,results:memories.slice(0,4).map((row,i)=>({memory_id:row.id,content:row.text,similarity_score:.91-i*.09,metadata:row.metadata,score_breakdown:{document_keyword_score:.23,document_vector_score:.81,graph_keyword_score:0,graph_vector_score:.45}}))};
      if(path==='page/graph/query')return graph;
      throw Error(`Unexpected synthetic POST: ${path}`);
    },
  };
}
