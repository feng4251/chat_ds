在本目录内,我想做一个网站,类似 chatgpt 的网页端,实现对 AgentModel 的网页聊天问答。左侧需要是历史对话,右侧是聊天问答气泡。需要可以在聊天输入框的右下角切换模型,或者你自动根据输入的是文字还是图片自动切换。其中内网可用的模型有如下两个:

cc@ubuntu:/home/yangbb$ curl 10.10.132.2:1025/v1/models
{"object":"list","data":[{"id":"AgentModel","object":"model","created":1778665143,"owned_by":"vllm","root":"/nfs/yangbb/Docker/Data/weights/Eco-Tech/DeepSeek-V4-Pro-w4a8-mtp","parent":null,"max_model_len":1048572,"permission":[{"id":"modelperm-93e8880ff5e27101","object":"model_permission","created":1778665143,"allow_create_engine":false,"allow_sampling":true,"allow_logprobs":true,"allow_search_indices":false,"allow_view":true,"allow_fine_tuning":false,"organization":"*","group":null,"is_blocking":false}]}]}
cc@ubuntu:/home/yangbb$ curl 10.10.132.125:1025/v1/models
{"object":"list","data":[{"id":"qwen3_6","object":"model","created":1778665158,"owned_by":"vllm","root":"/weights/model","parent":null,"max_model_len":262144,"permission":[{"id":"modelperm-a3b50bc64e71bd38","object":"model_permission","created":1778665158,"allow_create_engine":false,"allow_sampling":true,"allow_logprobs":true,"allow_search_indices":false,"allow_view":true,"allow_fine_tuning":false,"organization":"*","group":null,"is_blocking":false}]}]}


其中 AgentModel 为 deepseek-v4 文本模型,qwen3_6 为多模态模型。你需要增加可以根据 openai 或 antropic 兼容接口配置自定义模型的能力。在左侧第一次对话后,你就开始提取右侧历史会话中对本次会话的命名,向 chatgpt 所实现的功能。另外你应该还支持基于 agent harness 的网络检索和科学研究等功能,不同的功能可以通过 skills 实现并后续迭代扩展,每个功能可以在输入框的左下角以好看的形式下拉选择,你可以参考 chatgpt 或 claude 网页版的实现方式。你还应该接入数据库，一寸处不同用户的历史对话记录等信息，并可支持历史会话的重命名和删除操作，需要在首页增加用户的注册和登录功能。
