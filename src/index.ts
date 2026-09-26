import 'dotenv/config';
import { Telegraf, Markup } from 'telegraf';
import pg from 'pg';

const { Pool } = pg;
const BOT_TOKEN = process.env.BOT_TOKEN;
const DATABASE_URL = process.env.DATABASE_URL;

if (!BOT_TOKEN) throw new Error('BOT_TOKEN is not configured');
if (!DATABASE_URL) throw new Error('DATABASE_URL is not configured');

const bot = new Telegraf(BOT_TOKEN);
const pool = new Pool({ connectionString: DATABASE_URL, ssl: DATABASE_URL.includes('railway') ? { rejectUnauthorized:false } : undefined });

async function db(sql:string, params:any[]=[]){ return pool.query(sql, params); }

async function initDb(){
  await db(`
    CREATE TABLE IF NOT EXISTS users (
      id BIGSERIAL PRIMARY KEY,
      telegram_id BIGINT UNIQUE NOT NULL,
      username TEXT,
      first_name TEXT,
      created_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS transactions (
      id BIGSERIAL PRIMARY KEY,
      telegram_id BIGINT NOT NULL,
      type TEXT NOT NULL CHECK (type IN ('income','expense')),
      amount NUMERIC(14,2) NOT NULL,
      description TEXT DEFAULT '',
      created_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS tasks (
      id BIGSERIAL PRIMARY KEY,
      telegram_id BIGINT NOT NULL,
      title TEXT NOT NULL,
      done BOOLEAN DEFAULT FALSE,
      created_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS clients (
      id BIGSERIAL PRIMARY KEY,
      telegram_id BIGINT NOT NULL,
      name TEXT NOT NULL,
      contact TEXT DEFAULT '',
      created_at TIMESTAMPTZ DEFAULT NOW()
    );
  `);
}

async function ensureUser(ctx:any){
  const u=ctx.from;
  await db(
    'INSERT INTO users (telegram_id,username,first_name) VALUES ($1,$2,$3) ON CONFLICT (telegram_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name',
    [u.id,u.username??null,u.first_name??'']
  );
}

const menu = () => Markup.inlineKeyboard([
  [Markup.button.callback('💰 Финансы','finance'),Markup.button.callback('✅ Задачи','tasks')],
  [Markup.button.callback('👥 Клиенты','clients'),Markup.button.callback('📊 Аналитика','analytics')],
  [Markup.button.callback('🤖 AI-помощник','ai'),Markup.button.callback('⚙️ Настройки','settings')],
]);

bot.start(async ctx=>{
  await ensureUser(ctx);
  await ctx.reply(
    '🚀 BIZBOX\\n\\nУправляй. Развивай. Действуй.\\n\\nБизнес прямо в Telegram — финансы, задачи, клиенты и аналитика в одном месте.',
    menu()
  );
});

bot.command('menu',async ctx=>{ await ensureUser(ctx); await ctx.reply('🚀 Главное меню',menu()); });

bot.command('income',async ctx=>{
  await ensureUser(ctx);
  const text=ctx.message.text.replace(/^\\/income\\s*/,'').trim();
  const m=text.match(/^(\\d+(?:[.,]\\d+)?)\\s*(.*)$/);
  if(!m) return ctx.reply('Формат: /income 50000 Продажа');
  await db('INSERT INTO transactions (telegram_id,type,amount,description) VALUES ($1,\'income\',$2,$3)',[ctx.from.id,Number(m[1].replace(',','.')),m[2]]);
  return ctx.reply('✅ Доход добавлен.');
});

bot.command('expense',async ctx=>{
  await ensureUser(ctx);
  const text=ctx.message.text.replace(/^\\/expense\\s*/,'').trim();
  const m=text.match(/^(\\d+(?:[.,]\\d+)?)\\s*(.*)$/);
  if(!m) return ctx.reply('Формат: /expense 12000 Реклама');
  await db('INSERT INTO transactions (telegram_id,type,amount,description) VALUES ($1,\'expense\',$2,$3)',[ctx.from.id,Number(m[1].replace(',','.')),m[2]]);
  return ctx.reply('✅ Расход добавлен.');
});

bot.command('task',async ctx=>{
  await ensureUser(ctx);
  const title=ctx.message.text.replace(/^\\/task\\s*/,'').trim();
  if(!title) return ctx.reply('Формат: /task Позвонить клиенту');
  await db('INSERT INTO tasks (telegram_id,title) VALUES ($1,$2)',[ctx.from.id,title]);
  return ctx.reply('✅ Задача добавлена.');
});

bot.command('client',async ctx=>{
  await ensureUser(ctx);
  const text=ctx.message.text.replace(/^\\/client\\s*/,'').trim();
  if(!text) return ctx.reply('Формат: /client Имя, контакт');
  const [name,...rest]=text.split(',');
  await db('INSERT INTO clients (telegram_id,name,contact) VALUES ($1,$2,$3)',[ctx.from.id,name.trim(),rest.join(',').trim()]);
  return ctx.reply('✅ Клиент добавлен.');
});

bot.action('finance',async ctx=>{
  await ensureUser(ctx);
  const r=await db('SELECT COALESCE(SUM(amount) FILTER (WHERE type=\'income\'),0) income, COALESCE(SUM(amount) FILTER (WHERE type=\'expense\'),0) expense FROM transactions WHERE telegram_id=$1',[ctx.from.id]);
  const income=Number(r.rows[0].income), expense=Number(r.rows[0].expense);
  await ctx.answerCbQuery();
  await ctx.editMessageText(`💰 ФИНАНСЫ\\n\\n📈 Доход: ${income.toLocaleString('ru-RU')} ₸\\n📉 Расход: ${expense.toLocaleString('ru-RU')} ₸\\n💵 Баланс: ${(income-expense).toLocaleString('ru-RU')} ₸\\n\\nДобавить быстро:\\n/income 50000 Продажа\\n/expense 12000 Реклама`,menu());
});

bot.action('tasks',async ctx=>{
  await ensureUser(ctx);
  const r=await db('SELECT id,title,done FROM tasks WHERE telegram_id=$1 ORDER BY created_at DESC LIMIT 10',[ctx.from.id]);
  const lines=r.rows.length?r.rows.map((x:any)=>`${x.done?'☑️':'⬜'} ${x.title}`):['Пока задач нет.'];
  await ctx.answerCbQuery();
  await ctx.editMessageText('✅ ЗАДАЧИ\\n\\n'+lines.join('\\n')+'\\n\\nДобавить: /task Новая задача',menu());
});

bot.action('clients',async ctx=>{
  await ensureUser(ctx);
  const r=await db('SELECT name,contact FROM clients WHERE telegram_id=$1 ORDER BY created_at DESC LIMIT 10',[ctx.from.id]);
  const lines=r.rows.length?r.rows.map((x:any)=>`👤 ${x.name}${x.contact?' — '+x.contact:''}`):['Пока клиентов нет.'];
  await ctx.answerCbQuery();
  await ctx.editMessageText('👥 КЛИЕНТЫ\\n\\n'+lines.join('\\n')+'\\n\\nДобавить: /client Имя, контакт',menu());
});

bot.action('analytics',async ctx=>{
  await ensureUser(ctx);
  const r=await db('SELECT COUNT(*)::int transactions, COUNT(*) FILTER (WHERE type=\'income\')::int incomes, COUNT(*) FILTER (WHERE type=\'expense\')::int expenses FROM transactions WHERE telegram_id=$1',[ctx.from.id]);
  await ctx.answerCbQuery();
  await ctx.editMessageText(`📊 АНАЛИТИКА\\n\\nОпераций: ${r.rows[0].transactions}\\nДоходных: ${r.rows[0].incomes}\\nРасходных: ${r.rows[0].expenses}`,menu());
});

bot.action('ai',async ctx=>{
  await ctx.answerCbQuery();
  await ctx.reply('🤖 AI-помощник подключим следующим модулем. Он будет анализировать финансы, задачи и клиентов и выдавать конкретные действия.');
});

bot.action('settings',async ctx=>{
  await ctx.answerCbQuery();
  await ctx.editMessageText('⚙️ НАСТРОЙКИ\\n\\nBIZBOX v1.0\\nTelegram-first версия\\n\\nКоманды: /menu /income /expense /task /client',menu());
});

bot.catch((err,ctx)=>{ console.error('BOT ERROR',err); ctx.reply('⚠️ Произошла ошибка. Попробуй ещё раз.').catch(()=>{}); });

async function main(){
  await initDb();
  await bot.telegram.setMyCommands([
    {command:'menu',description:'Главное меню'},
    {command:'income',description:'Добавить доход'},
    {command:'expense',description:'Добавить расход'},
    {command:'task',description:'Добавить задачу'},
    {command:'client',description:'Добавить клиента'}
  ]);
  await bot.launch();
  console.log('BIZBOX bot started');
}
main().catch(err=>{ console.error(err); process.exit(1); });
process.once('SIGINT',()=>bot.stop('SIGINT'));
process.once('SIGTERM',()=>bot.stop('SIGTERM'));
