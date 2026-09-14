/** Friend tournament brackets: pairing and standings for a field of human and AI seats.
 * Pure and deterministic; the room service owns rooms, results and time. */
export const FORMATS=['single','robin'];
export const AI_NAMES=['Wire // AI','Volt // AI','Sable // AI','Glitch // AI','Nova // AI','Echo // AI','Rook // AI','Vex // AI'];

/** Adds AI seats so single elimination reaches a power of two and round robin an even count. */
export function fillField(players,format,ai=true){
 const field=players.map(p=>({...p}));
 const target=format==='single'?Math.max(2,2**Math.ceil(Math.log2(Math.max(2,field.length)))):field.length+(field.length%2);
 let n=0;
 while(ai&&field.length<target){field.push({id:`ai-${n+1}`,name:AI_NAMES[n%AI_NAMES.length]+(n>=AI_NAMES.length?` ${Math.floor(n/AI_NAMES.length)+1}`:''),ai:true});n++;}
 return field;
}
const match=(round,index,a,b)=>({id:`r${round+1}m${index+1}`,round,a,b,roomId:null,result:null});
/** Round-robin rounds by the circle method; a null seat is a bye. */
export function robinRounds(ids){
 const list=[...ids];if(list.length%2)list.push(null);
 const n=list.length,rounds=[];
 for(let r=0;r<n-1;r++){
  const pairs=[];
  for(let i=0;i<n/2;i++){const a=list[i],b=list[n-1-i];if(a!==null&&b!==null)pairs.push(r%2&&i===0?[b,a]:[a,b]);else pairs.push([a??b,null]);}
  rounds.push(pairs.map(([a,b],i)=>match(r,i,a,b)));
  list.splice(1,0,list.pop());
 }
 return rounds;
}
/** First single-elimination round in seed order (join order); later rounds pair winners in bracket order. */
export function firstRound(ids){
 const out=[];for(let i=0;i<ids.length;i+=2)out.push(match(0,i/2,ids[i],ids[i+1]??null));return out;
}
export function nextRound(previous,roundIndex){
 const winners=previous.map(m=>m.result?.winner??null);
 const out=[];for(let i=0;i<winners.length;i+=2)out.push(match(roundIndex,i/2,winners[i],winners[i+1]??null));
 return out;
}
export const roundComplete=round=>round.every(m=>m.result);
/** Wins, losses and ties from every decided match; ranked by wins, then fewest losses, then name. */
export function standings(players,rounds){
 const rows=new Map(players.map(p=>[p.id,{id:p.id,name:p.name,ai:!!p.ai,played:0,wins:0,losses:0,ties:0,rank:0}]));
 for(const round of rounds)for(const m of round){
  if(!m.result)continue;
  for(const id of [m.a,m.b]){const row=id&&rows.get(id);if(!row)continue;if(m.b===null||m.a===null){row.wins++;row.played++;continue;}row.played++;if(m.result.tie)row.ties++;else if(m.result.winner===id)row.wins++;else row.losses++;}
 }
 const sorted=[...rows.values()].sort((x,y)=>y.wins-x.wins||x.losses-y.losses||x.name.localeCompare(y.name));
 sorted.forEach((row,i)=>{const prev=sorted[i-1];row.rank=prev&&prev.wins===row.wins&&prev.losses===row.losses?prev.rank:i+1;});
 return sorted;
}
export function standingsText(name,rows){
 return [`Night City tournament · ${name}`,...rows.map(r=>`${r.rank}. ${r.name}${r.ai?' (AI)':''} · ${r.wins}W ${r.losses}L${r.ties?` ${r.ties}T`:''}`)].join('\n');
}
